"""Fan-out paralelo: un proceso por follower con sesión MT5 caliente + ACK."""

from __future__ import annotations

import multiprocessing as mp
import queue
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from config.settings import CopySettings, FollowerConfig
from core.connector import AccountConfig
from core.copier import TradeCopier
from models.copy_result import CopyAck, CopyResult
from models.position import Position
from utils.logger import setup_logger

if TYPE_CHECKING:
    from core.sync import PendingEvent


@dataclass
class CopyJob:
    """Trabajo serializable enviado del proceso master a un worker follower."""

    action: str  # open | close | modify | shutdown
    event_id: str = ""
    position: Position | None = None
    master_balance: float = 0.0
    attempt: int = 1

    @classmethod
    def shutdown(cls) -> CopyJob:
        return cls(action="shutdown")


def follower_worker(
    account: AccountConfig,
    copy_settings: CopySettings,
    job_queue: mp.Queue,
    ack_queue: mp.Queue,
) -> None:
    """
    Proceso dedicado a un follower.
    Mantiene la sesión MT5 abierta, procesa jobs FIFO y reporta CopyAck.
    """
    setup_logger()
    label = account.label
    logger.info(f"[WORKER {label}] Arrancado (PID={mp.current_process().pid})")

    copier = TradeCopier(account=account, copy_settings=copy_settings)
    if copier.ensure_ready():
        logger.info(f"[WORKER {label}] Sesión MT5 caliente lista")
    else:
        logger.error(f"[WORKER {label}] No se pudo conectar al arrancar — reintentará por job")

    try:
        while True:
            job: CopyJob = job_queue.get()
            if job is None or job.action == "shutdown":
                logger.info(f"[WORKER {label}] Shutdown recibido")
                break

            result: CopyResult
            master_ticket = job.position.ticket if job.position else 0

            try:
                if job.action == "open" and job.position is not None:
                    result = copier.open_position(job.position, job.master_balance)
                elif job.action == "close" and job.position is not None:
                    result = copier.close_position(job.position)
                elif job.action == "modify" and job.position is not None:
                    result = copier.modify_position(job.position)
                else:
                    logger.warning(f"[WORKER {label}] Job desconocido: action={job.action}")
                    result = CopyResult.fail(f"acción desconocida: {job.action}", retryable=False)
            except Exception as e:
                logger.exception(f"[WORKER {label}] Error procesando {job.action}: {e}")
                result = CopyResult.fail(str(e), retryable=True)

            if job.event_id:
                ack = CopyAck.from_result(
                    event_id=job.event_id,
                    follower=label,
                    action=job.action,
                    master_ticket=master_ticket,
                    result=result,
                    attempt=job.attempt,
                )
                try:
                    ack_queue.put(ack)
                except Exception as e:
                    logger.error(f"[WORKER {label}] No se pudo enviar ACK: {e}")
    finally:
        copier.shutdown()
        logger.info(f"[WORKER {label}] Detenido")


class FollowerFanout:
    """Lanza un proceso por follower y reparte cada evento en paralelo."""

    def __init__(self, followers: list[FollowerConfig]):
        self._followers = followers
        self._ctx = mp.get_context("spawn")
        self._queues: list[mp.Queue] = []
        self._label_to_queue: dict[str, mp.Queue] = {}
        self._processes: list[mp.Process] = []
        self._ack_queue: mp.Queue | None = None

    @property
    def worker_count(self) -> int:
        return len(self._processes)

    @property
    def follower_labels(self) -> list[str]:
        return [f.account.label for f in self._followers]

    def start(self) -> None:
        self._ack_queue = self._ctx.Queue()

        for follower in self._followers:
            job_queue = self._ctx.Queue()
            process = self._ctx.Process(
                target=follower_worker,
                args=(follower.account, follower.copy_settings, job_queue, self._ack_queue),
                name=f"follower-{follower.account.label}",
                daemon=True,
            )
            process.start()
            self._queues.append(job_queue)
            self._label_to_queue[follower.account.label] = job_queue
            self._processes.append(process)
            logger.info(
                f"[FANOUT] Worker '{follower.account.label}' PID={process.pid}"
            )

        logger.info(f"[FANOUT] {len(self._processes)} follower(s) en paralelo (sesión caliente + ACK)")

    def dispatch_job(self, event: PendingEvent, follower_labels: list[str] | None = None) -> None:
        """Encola un CopyJob a todos los followers o a un subconjunto (retries)."""
        labels = follower_labels if follower_labels is not None else list(self._label_to_queue.keys())
        for label in labels:
            q = self._label_to_queue.get(label)
            if q is None:
                continue
            st = event.followers.get(label)
            attempt = st.attempt if st else 1
            job = CopyJob(
                action=event.action,
                event_id=event.event_id,
                position=event.position,
                master_balance=event.master_balance,
                attempt=attempt,
            )
            q.put(job)

    def drain_acks(self, max_items: int = 500) -> list[CopyAck]:
        if self._ack_queue is None:
            return []
        acks: list[CopyAck] = []
        for _ in range(max_items):
            try:
                acks.append(self._ack_queue.get_nowait())
            except queue.Empty:
                break
        return acks

    def stop(self, join_timeout: float = 10.0) -> None:
        for q in self._queues:
            try:
                q.put(CopyJob.shutdown())
            except Exception:
                pass

        for process in self._processes:
            process.join(timeout=join_timeout)
            if process.is_alive():
                logger.warning(f"[FANOUT] Forzando stop de {process.name}")
                process.terminate()
                process.join(timeout=2.0)

        self._queues.clear()
        self._label_to_queue.clear()
        self._processes.clear()
        self._ack_queue = None
        logger.info("[FANOUT] Todos los workers detenidos")
