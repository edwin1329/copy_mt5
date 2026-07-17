"""Coordinador de ACK: registra eventos, recibe confirmaciones y reintenta."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from models.copy_result import CopyAck, FollowerAckState
from models.position import Position

STATE_DIR = Path(__file__).parent.parent / "state"
PENDING_FILE = STATE_DIR / "pending_events.json"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_ACK_TIMEOUT_S = 20.0
DEFAULT_BACKOFF_BASE_S = 2.0
DEFAULT_MAX_BACKOFF_S = 30.0


def _new_event_id() -> str:
    return uuid.uuid4().hex[:10]


def _position_to_dict(pos: Position) -> dict:
    return {
        "ticket": pos.ticket,
        "symbol": pos.symbol,
        "order_type": pos.order_type,
        "volume": pos.volume,
        "open_price": pos.open_price,
        "sl": pos.sl,
        "tp": pos.tp,
        "comment": pos.comment,
    }


def _position_from_dict(data: dict) -> Position:
    return Position(
        ticket=int(data["ticket"]),
        symbol=data["symbol"],
        order_type=int(data["order_type"]),
        volume=float(data["volume"]),
        open_price=float(data["open_price"]),
        sl=float(data["sl"]),
        tp=float(data["tp"]),
        comment=data.get("comment", ""),
    )


@dataclass
class PendingEvent:
    event_id: str
    action: str
    position: Position
    master_balance: float
    followers: dict[str, FollowerAckState] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def master_ticket(self) -> int:
        return self.position.ticket

    def is_complete(self) -> bool:
        return all(s.status in ("ok", "fail_final") for s in self.followers.values())

    def pending_followers(self) -> list[str]:
        return [name for name, s in self.followers.items() if s.status == "pending"]

    def retry_ready_followers(self, now: float) -> list[str]:
        ready = []
        for name, s in self.followers.items():
            if s.status == "fail_retry" and now >= s.next_retry_at:
                ready.append(name)
        return ready

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "position": _position_to_dict(self.position),
            "master_balance": self.master_balance,
            "created_at": self.created_at,
            "followers": {
                name: {
                    "status": st.status,
                    "attempt": st.attempt,
                    "retcode": st.retcode,
                    "detail": st.detail,
                    "next_retry_at": st.next_retry_at,
                    "last_update": st.last_update,
                }
                for name, st in self.followers.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> PendingEvent:
        followers = {}
        for name, st in data.get("followers", {}).items():
            followers[name] = FollowerAckState(
                status=st.get("status", "pending"),
                attempt=int(st.get("attempt", 0)),
                retcode=st.get("retcode"),
                detail=st.get("detail", ""),
                next_retry_at=float(st.get("next_retry_at", 0.0)),
                last_update=float(st.get("last_update", 0.0)),
            )
        return cls(
            event_id=data["event_id"],
            action=data["action"],
            position=_position_from_dict(data["position"]),
            master_balance=float(data.get("master_balance", 0.0)),
            followers=followers,
            created_at=float(data.get("created_at", time.time())),
        )


class SyncCoordinator:
    """
    Registra eventos de copy, espera ACK por follower y reintenta fallos temporales.
    Persiste pendientes en state/pending_events.json para sobrevivir reinicios.
    """

    def __init__(
        self,
        follower_labels: list[str],
        fanout,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        ack_timeout_s: float = DEFAULT_ACK_TIMEOUT_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        state_path: Path = PENDING_FILE,
    ):
        self._follower_labels = list(follower_labels)
        self._fanout = fanout
        self.max_attempts = max_attempts
        self.ack_timeout_s = ack_timeout_s
        self.backoff_base_s = backoff_base_s
        self.max_backoff_s = max_backoff_s
        self.state_path = state_path
        self._pending: dict[str, PendingEvent] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def load_and_resume(self) -> int:
        """Carga pendientes del disco y re-despacha los que no están ok/fail_final."""
        if not self.state_path.exists():
            return 0

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[ACK] No se pudo leer {self.state_path}: {e}")
            return 0

        loaded = 0
        now = time.monotonic()
        for item in raw.get("events", []):
            try:
                event = PendingEvent.from_dict(item)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"[ACK] Evento inválido en state, se omite: {e}")
                continue

            if event.is_complete():
                continue

            # Al reiniciar, pending/fail_retry vuelven a cola con pequeño delay
            for label, st in event.followers.items():
                if label not in self._follower_labels:
                    st.status = "fail_final"
                    st.detail = "follower ya no está en config"
                    continue
                if st.status in ("pending", "fail_retry"):
                    st.status = "fail_retry"
                    st.next_retry_at = now  # reintentar ya
            self._pending[event.event_id] = event
            loaded += 1

        if loaded:
            logger.info(f"[ACK] Reanudando {loaded} evento(s) pendiente(s) desde disco")
            self.tick()
        return loaded

    def submit_batch(
        self,
        opened: list[Position],
        closed: list[Position],
        modified: list[Position],
        master_balance: float,
    ) -> list[str]:
        """Crea PendingEvents y despacha el primer intento a todos los followers."""
        event_ids: list[str] = []
        for pos in opened:
            event_ids.append(self._submit("open", pos, master_balance))
        for pos in closed:
            event_ids.append(self._submit("close", pos, master_balance))
        for pos in modified:
            event_ids.append(self._submit("modify", pos, master_balance))
        self._persist()
        return event_ids

    def _submit(self, action: str, position: Position, master_balance: float) -> str:
        event_id = _new_event_id()
        now = time.monotonic()
        followers = {
            label: FollowerAckState(status="pending", attempt=1, last_update=now)
            for label in self._follower_labels
        }
        event = PendingEvent(
            event_id=event_id,
            action=action,
            position=position,
            master_balance=master_balance,
            followers=followers,
        )
        self._pending[event_id] = event
        logger.info(
            f"[ACK] Nuevo evento {event_id} | {action} {position.symbol} "
            f"ticket={position.ticket} → {len(followers)} follower(s)"
        )
        self._fanout.dispatch_job(event, follower_labels=None)
        return event_id

    def process_acks(self) -> int:
        """Drena la cola de ACK del fanout y actualiza estado."""
        acks = self._fanout.drain_acks()
        if not acks:
            return 0

        for ack in acks:
            self._apply_ack(ack)

        self._persist()
        return len(acks)

    def _apply_ack(self, ack: CopyAck) -> None:
        event = self._pending.get(ack.event_id)
        if event is None:
            logger.debug(f"[ACK] ACK huérfano ignorado: event_id={ack.event_id} follower={ack.follower}")
            return

        state = event.followers.get(ack.follower)
        if state is None:
            logger.warning(f"[ACK] Follower desconocido en ACK: {ack.follower}")
            return

        now = time.monotonic()
        state.last_update = now
        state.retcode = ack.retcode
        state.detail = ack.detail
        state.attempt = max(state.attempt, ack.attempt)

        if ack.success:
            state.status = "ok"
            extra = " (idempotente)" if "already" in (ack.detail or "").lower() else ""
            logger.info(
                f"[ACK] OK {ack.follower} | {event.action} ticket={event.master_ticket} "
                f"event={event.event_id}{extra}"
            )
        elif ack.retryable and state.attempt < self.max_attempts:
            delay = min(self.backoff_base_s * (2 ** max(state.attempt - 1, 0)), self.max_backoff_s)
            state.status = "fail_retry"
            state.next_retry_at = now + delay
            logger.warning(
                f"[ACK] Retry {ack.follower} | {event.action} ticket={event.master_ticket} "
                f"attempt={state.attempt}/{self.max_attempts} en {delay:.1f}s | {ack.detail}"
            )
        else:
            state.status = "fail_final"
            logger.error(
                f"[ACK] FAIL {ack.follower} | {event.action} ticket={event.master_ticket} "
                f"event={event.event_id} | {ack.detail} retcode={ack.retcode}"
            )

        if event.is_complete():
            self._finalize(event)

    def tick(self) -> None:
        """Timeouts de pending + re-despacho de fail_retry listos."""
        now = time.monotonic()
        changed = False

        for event in list(self._pending.values()):
            # Timeout: pending demasiado tiempo sin ACK
            for label, st in event.followers.items():
                if st.status == "pending" and (now - st.last_update) >= self.ack_timeout_s:
                    if st.attempt < self.max_attempts:
                        delay = min(self.backoff_base_s * (2 ** max(st.attempt - 1, 0)), self.max_backoff_s)
                        st.status = "fail_retry"
                        st.detail = "ack timeout"
                        st.next_retry_at = now + delay
                        logger.warning(
                            f"[ACK] Timeout {label} | {event.action} ticket={event.master_ticket} "
                            f"event={event.event_id} — reintento en {delay:.1f}s"
                        )
                        changed = True
                    else:
                        st.status = "fail_final"
                        st.detail = "ack timeout (max attempts)"
                        logger.error(
                            f"[ACK] FAIL timeout {label} | {event.action} ticket={event.master_ticket}"
                        )
                        changed = True

            # Re-despachar retries listos
            ready = event.retry_ready_followers(now)
            if ready:
                for label in ready:
                    st = event.followers[label]
                    st.attempt += 1
                    st.status = "pending"
                    st.last_update = now
                self._fanout.dispatch_job(event, follower_labels=ready)
                logger.info(
                    f"[ACK] Re-despacho event={event.event_id} → {ready}"
                )
                changed = True

            if event.is_complete():
                self._finalize(event)
                changed = True

        if changed:
            self._persist()

    def _finalize(self, event: PendingEvent) -> None:
        ok = sum(1 for s in event.followers.values() if s.status == "ok")
        fail = sum(1 for s in event.followers.values() if s.status == "fail_final")
        logger.info(
            f"[ACK] Evento cerrado {event.event_id} | {event.action} "
            f"ticket={event.master_ticket} | ok={ok} fail={fail}"
        )
        self._pending.pop(event.event_id, None)

    def _persist(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "events": [e.to_dict() for e in self._pending.values()],
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            logger.error(f"[ACK] No se pudo persistir state: {e}")
