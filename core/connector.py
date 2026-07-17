import time
from dataclasses import dataclass
from loguru import logger

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


@dataclass
class AccountConfig:
    login: int
    password: str
    server: str
    path: str
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = str(self.login)


class MT5Connector:
    """Maneja la conexión a un terminal MT5. Solo puede haber una conexión activa por proceso."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    # Cuentas que fallaron en algún momento — para loguear INFO al reconectar
    _failed_accounts: set[int] = set()

    def __init__(self, account: AccountConfig):
        self.account = account
        self._connected = False

    def connect(self) -> bool:
        if mt5 is None:
            logger.error("La librería MetaTrader5 no está instalada (solo funciona en Windows).")
            return False

        if self._connected:
            mt5.shutdown()
            self._connected = False

        for attempt in range(1, self.MAX_RETRIES + 1):
            initialized = mt5.initialize(
                path=self.account.path,
                login=self.account.login,
                password=self.account.password,
                server=self.account.server,
            )
            if initialized:
                info = mt5.account_info()
                if info:
                    login = self.account.login
                    msg = f"[{self.account.label}] Conectado | Balance: {info.balance:.2f} {info.currency}"
                    if login in MT5Connector._failed_accounts:
                        logger.info(msg)
                        MT5Connector._failed_accounts.discard(login)
                    else:
                        logger.debug(msg)
                    self._connected = True
                    return True

            error = mt5.last_error() if mt5 else ("", "librería no disponible")
            logger.warning(
                f"[{self.account.label}] Intento {attempt}/{self.MAX_RETRIES} fallido: {error}"
            )
            MT5Connector._failed_accounts.add(self.account.login)
            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_DELAY)

        logger.error(f"[{self.account.label}] No se pudo conectar después de {self.MAX_RETRIES} intentos.")
        return False

    def ensure_connected(self) -> bool:
        """Reutiliza la sesión si sigue viva; reconecta solo si es necesario."""
        if mt5 is None:
            logger.error("La librería MetaTrader5 no está instalada (solo funciona en Windows).")
            return False

        if self._connected:
            info = mt5.account_info()
            if info is not None and info.login == self.account.login:
                return True
            logger.warning(f"[{self.account.label}] Sesión caída — reconectando...")
            self._connected = False
            try:
                mt5.shutdown()
            except Exception:
                pass

        return self.connect()

    def disconnect(self) -> None:
        if mt5 and self._connected:
            mt5.shutdown()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
