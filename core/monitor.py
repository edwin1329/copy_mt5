import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, Tuple
from loguru import logger

from models.position import Position
from core.connector import MT5Connector, AccountConfig

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

SIGNAL_FILENAME = "copy_mt5_signal.txt"


class _SignalHandler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    def __init__(self, signal_file: Path, event: threading.Event):
        if _WATCHDOG_AVAILABLE:
            super().__init__()
        self._name = signal_file.name
        self._event = event

    def on_modified(self, fs_event):
        if Path(fs_event.src_path).name == self._name:
            self._event.set()

    def on_created(self, fs_event):
        if Path(fs_event.src_path).name == self._name:
            self._event.set()


class MasterMonitor:
    """
    Modos de operación:
      - Evento: watchdog observa el archivo que escribe el EA TradeSignaler.
        Python duerme hasta que el EA señaliza un cambio en la cuenta.
      - Fallback: red de seguridad periódica (por defecto larga, p.ej. 30s)
        por si se pierde una señal del EA.

    Mantiene una sesión MT5 caliente: no hace initialize/shutdown en cada poll.
    """

    def __init__(self, account: AccountConfig, fallback_interval: float = 30.0):
        self.account = account
        self.fallback_interval = fallback_interval
        self.connector = MT5Connector(account)
        self._prev_snapshot: Dict[int, Position] = {}
        self._event = threading.Event()
        self._observer = None
        self._event_mode = False
        self._signal_file: Path | None = None
        self._ea_warned = False

    # ── inicio / parada ──────────────────────────────────────────────────────

    def start(self) -> bool:
        if not _WATCHDOG_AVAILABLE:
            logger.warning(
                "watchdog no instalado — usando polling cada {:.1f}s.".format(self.fallback_interval)
            )
            return False

        signal_dir = self._get_signal_dir()
        if signal_dir is None:
            logger.warning("No se pudo obtener la ruta común de MT5 — usando polling.")
            return False

        signal_dir.mkdir(parents=True, exist_ok=True)
        self._signal_file = signal_dir / SIGNAL_FILENAME

        handler = _SignalHandler(self._signal_file, self._event)
        self._observer = Observer()
        self._observer.schedule(handler, str(signal_dir), recursive=False)
        self._observer.start()
        self._event_mode = True

        logger.info(f"Monitor modo EVENTO activo | archivo: {self._signal_file}")
        self._log_ea_status()
        return True

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self.connector.disconnect()
        logger.debug("[MASTER] Sesión MT5 cerrada")

    # ── espera de señal ──────────────────────────────────────────────────────

    def wait(self, timeout: float | None = None) -> bool:
        """
        Bloquea hasta recibir señal del EA o hasta timeout.
        `timeout` opcional (p.ej. más corto si hay ACK pendientes).
        Retorna True si despertó por señal del EA, False si fue el fallback.
        """
        wait_s = self.fallback_interval if timeout is None else timeout
        triggered = self._event.wait(timeout=wait_s)
        self._event.clear()

        if triggered and self._ea_warned:
            logger.info("[EA TradeSignaler] Señal recibida — EA activo y funcionando.")
            self._ea_warned = False

        return triggered

    # ── detección de cambios ──────────────────────────────────────────────────

    def get_changes(self) -> Tuple[list[Position], list[Position], list[Position], float]:
        """
        Diff de posiciones usando la sesión caliente.
        Retorna (opened, closed, modified, master_balance).
        El balance se lee en la misma conexión (sin reconnect extra).
        """
        if not self.connector.ensure_connected():
            return [], [], [], 0.0

        current = self._snapshot()
        prev_tickets: Set[int] = set(self._prev_snapshot.keys())
        curr_tickets: Set[int] = set(current.keys())

        opened = [current[t] for t in curr_tickets - prev_tickets]
        closed = [self._prev_snapshot[t] for t in prev_tickets - curr_tickets]
        modified = [
            current[t]
            for t in curr_tickets & prev_tickets
            if current[t] != self._prev_snapshot[t]
        ]

        if opened:
            logger.info(f"[MASTER] {len(opened)} nueva(s): {[p.symbol for p in opened]}")
        if closed:
            logger.info(f"[MASTER] {len(closed)} cerrada(s): {[p.symbol for p in closed]}")
        if modified:
            logger.info(f"[MASTER] {len(modified)} modificada(s): {[p.symbol for p in modified]}")

        balance = self._read_balance() if (opened or closed or modified) else 0.0

        self._prev_snapshot = current
        return opened, closed, modified, balance

    def initialize_snapshot(self) -> None:
        if not self.connector.ensure_connected():
            return
        self._prev_snapshot = self._snapshot()
        logger.info(
            f"[MASTER] Snapshot inicial: {len(self._prev_snapshot)} posición(es) abiertas "
            f"| sesión caliente activa"
        )

    # ── estado del EA ────────────────────────────────────────────────────────

    def _log_ea_status(self) -> None:
        """Revisa el archivo de señal al arrancar e informa el estado del EA."""
        if self._signal_file is None:
            return

        if not self._signal_file.exists():
            logger.warning(
                "[EA TradeSignaler] Archivo de señal NO encontrado.\n"
                "  → Adjunta el EA 'TradeSignaler' a cualquier gráfico del terminal MASTER.\n"
                f"  → Ruta esperada: {self._signal_file}\n"
                "  → El monitor funciona en modo fallback "
                f"(verifica cada {self.fallback_interval:.0f}s) hasta que el EA se active."
            )
            self._ea_warned = True
            return

        # El archivo existe → el EA cargó al menos una vez. No importa cuándo fue la
        # última escritura: si no hay operaciones, OnTrade() no se dispara y el archivo
        # queda estático. Eso es comportamiento normal.
        mtime = self._signal_file.stat().st_mtime
        last_update = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        self._last_ea_signal = time.monotonic()
        logger.info(f"[EA TradeSignaler] Activo ✓ | Última escritura: {last_update}")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _read_balance(self) -> float:
        if mt5 is None or not self.connector.is_connected:
            return 0.0
        info = mt5.account_info()
        return info.balance if info else 0.0

    def _snapshot(self) -> Dict[int, Position]:
        if mt5 is None or not self.connector.is_connected:
            return {}
        raw = mt5.positions_get()
        if raw is None:
            return {}
        return {
            p.ticket: Position(
                ticket=p.ticket,
                symbol=p.symbol,
                order_type=p.type,
                volume=p.volume,
                open_price=p.price_open,
                sl=p.sl,
                tp=p.tp,
                comment=p.comment,
            )
            for p in raw
        }

    def _get_signal_dir(self) -> Path | None:
        if not self.connector.ensure_connected():
            return None
        info = mt5.terminal_info()
        if info:
            return Path(info.commondata_path) / "Files"
        return None
