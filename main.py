import time
import signal
import sys

from loguru import logger

from utils.logger import setup_logger
from config.settings import load_settings
from core.monitor import MasterMonitor
from core.worker import FollowerFanout
from core.sync import SyncCoordinator

_running = True


def _handle_stop(signum, frame):
    global _running
    logger.info("Señal de parada recibida. Cerrando...")
    _running = False


def main() -> None:
    setup_logger()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Error de configuración: {e}")
        sys.exit(1)

    cs = settings.copy
    logger.info(f"Iniciando CopyMT5 | Master: {settings.master.label} | Followers: {len(settings.followers)}")
    for f in settings.followers:
        fcs = f.copy_settings
        logger.info(
            f"  [{f.account.label}] recalculate={fcs.recalculate_lot} mode={fcs.lot_mode} value={fcs.lot_value}"
        )

    fanout = FollowerFanout(settings.followers)
    fanout.start()

    coordinator = SyncCoordinator(fanout.follower_labels, fanout)
    resumed = coordinator.load_and_resume()
    if resumed:
        logger.info(f"[ACK] {resumed} evento(s) reanudado(s)")

    monitor = MasterMonitor(settings.master, fallback_interval=cs.fallback_interval)
    monitor.initialize_snapshot()

    event_mode = monitor.start()

    if event_mode:
        logger.info(
            f"Modo: EVENTO (watchdog) | Fallback seguridad cada {cs.fallback_interval}s "
            "| master sesión caliente | ACK+retry activo"
        )
    else:
        logger.info(
            f"Modo: POLLING cada {cs.poll_interval}s | master sesión caliente | ACK+retry activo"
        )

    logger.info(
        f"Monitoreo activo | fan-out paralelo={fanout.worker_count} workers. "
        "Presiona Ctrl+C para detener."
    )

    try:
        while _running:
            try:
                # Con eventos ACK pendientes, despertar más seguido para retries/timeouts
                if event_mode:
                    wait_s = 1.0 if coordinator.pending_count else cs.fallback_interval
                    by_ea = monitor.wait(timeout=wait_s)
                    if by_ea:
                        logger.debug("[EA] Señal recibida — verificando posiciones.")
                    elif coordinator.pending_count:
                        logger.debug("[ACK] Tick pendientes — procesando ACKs/retries.")
                    else:
                        logger.debug("[MASTER] Fallback idle — verificación de seguridad.")
                else:
                    sleep_s = min(cs.poll_interval, 1.0) if coordinator.pending_count else cs.poll_interval
                    time.sleep(sleep_s)

                # Procesar ACKs y reintentos aunque no haya cambios nuevos
                coordinator.process_acks()
                coordinator.tick()

                opened, closed, modified, master_balance = monitor.get_changes()

                if opened or closed or modified:
                    coordinator.submit_batch(opened, closed, modified, master_balance)
                    # Drenar ACKs rápidos del primer intento
                    coordinator.process_acks()

            except Exception as e:
                logger.exception(f"Error inesperado en el loop principal: {e}")
                time.sleep(1.0)
    finally:
        # Última pasada de ACKs antes de bajar workers
        try:
            coordinator.process_acks()
            coordinator.tick()
        except Exception:
            pass
        monitor.stop()
        fanout.stop()
        pending = coordinator.pending_count
        if pending:
            logger.warning(
                f"[ACK] Quedan {pending} evento(s) pendiente(s) en state/pending_events.json "
                "(se reanudarán al reiniciar)"
            )
        logger.info("CopyMT5 detenido.")


if __name__ == "__main__":
    main()
