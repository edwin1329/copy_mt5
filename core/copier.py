from loguru import logger

from models.position import Position
from models.copy_result import CopyResult
from core.connector import MT5Connector, AccountConfig
from config.settings import CopySettings

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

# Códigos de retorno MT5 más comunes con descripción legible
_RETCODE_MSG = {
    10004: "Requote",
    10006: "Solicitud rechazada",
    10007: "Solicitud cancelada por el trader",
    10010: "Solo parte de la solicitud fue completada",
    10011: "Error genérico de procesamiento",
    10012: "Solicitud cancelada por timeout",
    10013: "Solicitud inválida",
    10014: "Volumen inválido",
    10015: "Precio inválido",
    10016: "Stops inválidos (SL/TP)",
    10017: "Trading desactivado en la cuenta",
    10018: "Mercado cerrado",
    10019: "Fondos insuficientes",
    10020: "Precios cambiaron",
    10021: "Sin cotizaciones disponibles",
    10024: "Demasiadas solicitudes",
    10025: "Sin cambios en la solicitud",
    10026: "AutoTrading desactivado por el servidor",
    10027: "AutoTrading desactivado en el terminal (activa el botón AutoTrading)",
    10028: "Operación bloqueada para procesamiento",
    10029: "Orden o posición congelada",
    10030: "Modo de filling no soportado",
}


def _retcode_desc(code: int) -> str:
    return _RETCODE_MSG.get(code, f"error desconocido ({code})")


_FILLING_MODES = None  # se inicializa en tiempo de ejecución


def _filling_modes():
    global _FILLING_MODES
    if _FILLING_MODES is None:
        _FILLING_MODES = [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    return _FILLING_MODES


def _order_send(request: dict, label: str):
    """
    Envía la orden probando los tres modos de filling en orden.
    Si el broker rechaza uno con retcode=10030, pasa al siguiente automáticamente.
    """
    modes = _filling_modes()
    result = None
    for mode in modes:
        request["type_filling"] = mode
        result = mt5.order_send(request)
        if result is None:
            break
        if result.retcode != 10030:
            return result
        logger.debug(f"{label} Filling mode {mode} rechazado, probando siguiente...")
    return result


def _calc_lot(master_volume: float, cs: CopySettings, master_balance: float, follower_balance: float, symbol: str) -> float:
    symbol_rule = cs.find_symbol_rule(symbol)
    if symbol_rule is not None:
        resolved = symbol_rule.resolve_lot(master_volume)
        if resolved is not None:
            return resolved
        return master_volume

    if not cs.recalculate_lot:
        lot = master_volume

    elif cs.lot_mode == "range":
        lot = None
        for r in cs.lot_ranges:
            if r.matches(master_volume):
                lot = r.lot
                break
        if lot is None:
            logger.warning(f"Volumen {master_volume} no encaja en ningún rango. Usando lot_value={cs.lot_value}.")
            lot = cs.lot_value

    elif cs.lot_mode == "multiplier":
        lot = round(master_volume * cs.lot_value, 2)

    elif cs.lot_mode == "proportional":
        if master_balance <= 0:
            lot = cs.lot_value
        else:
            ratio = follower_balance / master_balance
            lot = round(master_volume * ratio, 2)

    else:
        lot = cs.lot_value

    if cs.volatility_lot_boost.applies_to(symbol, master_volume):
        lot = round(lot + cs.volatility_lot_boost.extra_lot, 2)

    return lot


def _get_balance() -> float:
    if mt5 is None:
        return 0.0
    info = mt5.account_info()
    return info.balance if info else 0.0


def _copy_tag(master_ticket: int) -> str:
    return f"copy#{master_ticket}"


def _comment_matches_tag(comment: str | None, tag: str) -> bool:
    """Match estricto: comment exactamente igual al tag, o tag como token completo."""
    if not comment:
        return False
    c = comment.strip()
    if c == tag:
        return True
    # Algunos brokers añaden sufijos; exigir frontera no-numérica tras el ticket
    if c.startswith(tag):
        rest = c[len(tag):]
        return len(rest) == 0 or not rest[0].isdigit()
    return False


def _find_follower_position(master_ticket: int):
    if mt5 is None:
        return None
    positions = mt5.positions_get()
    if not positions:
        return None
    tag = _copy_tag(master_ticket)
    for p in positions:
        if _comment_matches_tag(p.comment, tag):
            return p
    return None


class TradeCopier:
    """Replica operaciones del master hacia una cuenta follower (sesión MT5 persistente)."""

    def __init__(self, account: AccountConfig, copy_settings: CopySettings):
        self.account = account
        self.cs = copy_settings
        self.connector = MT5Connector(account)

    def _label(self) -> str:
        return f"[FOLLOWER {self.account.label}]"

    def ensure_ready(self) -> bool:
        return self.connector.ensure_connected()

    def shutdown(self) -> None:
        self.connector.disconnect()

    def _log_error(self, action: str, symbol: str, result) -> None:
        if result is None:
            logger.error(f"{self._label()} {action} {symbol}: sin respuesta del terminal.")
            return
        code = result.retcode
        desc = _retcode_desc(code)
        logger.error(f"{self._label()} {action} {symbol}: {desc} | retcode={code}")

    def open_position(self, pos: Position, master_balance: float) -> CopyResult:
        if not self.ensure_ready():
            return CopyResult.fail("no se pudo conectar", retryable=True)

        existing = _find_follower_position(pos.ticket)
        if existing is not None:
            logger.info(
                f"{self._label()} Open idempotente: ya existe copy#{pos.ticket} "
                f"(follower ticket={existing.ticket})"
            )
            return CopyResult.ok(
                follower_lot=existing.volume,
                follower_ticket=existing.ticket,
                already_exists=True,
                detail="already_exists",
            )

        follower_balance = _get_balance()
        lot = _calc_lot(pos.volume, self.cs, master_balance, follower_balance, pos.symbol)

        symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            logger.warning(f"{self._label()} Símbolo {pos.symbol} no encontrado en este broker.")
            return CopyResult.fail(f"símbolo {pos.symbol} no encontrado", retryable=False)

        if not symbol_info.visible:
            mt5.symbol_select(pos.symbol, True)

        min_lot = symbol_info.volume_min
        if lot < min_lot:
            logger.info(
                f"{self._label()} Lote calculado {lot} < mínimo permitido {min_lot} para {pos.symbol}. Ajustando a {min_lot}."
            )
            lot = min_lot

        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.warning(f"{self._label()} No se pudo obtener tick para {pos.symbol}.")
            return CopyResult.fail(f"sin tick para {pos.symbol}", retryable=True)

        price = tick.ask if pos.is_buy else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if pos.is_buy else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": pos.sl,
            "tp": pos.tp,
            "deviation": self.cs.max_slippage,
            "magic": 999001,
            "comment": _copy_tag(pos.ticket),
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = _order_send(request, self._label())
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.success(
                f"{self._label()} Abierta: {pos.symbol} {'BUY' if pos.is_buy else 'SELL'} "
                f"{lot} lotes (master={pos.volume}) | ticket={result.order}"
            )
            return CopyResult.ok(
                follower_lot=lot,
                follower_ticket=result.order,
                retcode=result.retcode,
            )

        self._log_error("Error al abrir", pos.symbol, result)
        retcode = result.retcode if result else None
        desc = _retcode_desc(retcode) if retcode is not None else "sin respuesta"
        return CopyResult.from_retcode(retcode, desc)

    def close_position(self, pos: Position) -> CopyResult:
        if not self.ensure_ready():
            return CopyResult.fail("no se pudo conectar", retryable=True)

        existing = _find_follower_position(pos.ticket)
        if existing is None:
            logger.warning(f"{self._label()} No se encontró posición para cerrar (master ticket={pos.ticket}).")
            # Puede ser race: open aún no llegó → reintentar
            return CopyResult.fail(
                f"posición copy#{pos.ticket} no encontrada",
                retryable=True,
            )

        follower_ticket = existing.ticket
        fp = existing

        tick = mt5.symbol_info_tick(fp.symbol)
        if tick is None:
            return CopyResult.fail(f"sin tick para {fp.symbol}", retryable=True)

        close_type = mt5.ORDER_TYPE_SELL if fp.type == 0 else mt5.ORDER_TYPE_BUY
        price = tick.bid if fp.type == 0 else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": fp.symbol,
            "volume": fp.volume,
            "type": close_type,
            "position": follower_ticket,
            "price": price,
            "deviation": self.cs.max_slippage,
            "magic": 999001,
            "comment": f"close#{pos.ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = _order_send(request, self._label())
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.success(f"{self._label()} Cerrada: {fp.symbol} ticket={follower_ticket}")
            return CopyResult.ok(follower_ticket=follower_ticket, follower_lot=fp.volume, retcode=result.retcode)

        self._log_error("Error al cerrar", fp.symbol, result)
        retcode = result.retcode if result else None
        desc = _retcode_desc(retcode) if retcode is not None else "sin respuesta"
        return CopyResult.from_retcode(retcode, desc)

    def modify_position(self, pos: Position) -> CopyResult:
        if not self.ensure_ready():
            return CopyResult.fail("no se pudo conectar", retryable=True)

        existing = _find_follower_position(pos.ticket)
        if existing is None:
            logger.warning(f"{self._label()} No se encontró posición para modificar (master ticket={pos.ticket}).")
            return CopyResult.fail(
                f"posición copy#{pos.ticket} no encontrada",
                retryable=True,
            )

        follower_ticket = existing.ticket

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": follower_ticket,
            "sl": pos.sl,
            "tp": pos.tp,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.success(f"{self._label()} SL/TP modificado: ticket={follower_ticket} sl={pos.sl} tp={pos.tp}")
            return CopyResult.ok(follower_ticket=follower_ticket, retcode=result.retcode)

        self._log_error("Error al modificar", pos.symbol, result)
        retcode = result.retcode if result else None
        desc = _retcode_desc(retcode) if retcode is not None else "sin respuesta"
        return CopyResult.from_retcode(retcode, desc)
