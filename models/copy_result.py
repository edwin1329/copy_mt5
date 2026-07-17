from __future__ import annotations

from dataclasses import dataclass, field


# Retcodes MT5 que suelen recuperarse con reintento
RETRYABLE_RETCODES = frozenset({
    10004,  # Requote
    10012,  # Timeout
    10019,  # Fondos insuficientes (a veces se libera margen)
    10020,  # Precios cambiaron
    10021,  # Sin cotizaciones
    10024,  # Demasiadas solicitudes
    10028,  # Operación bloqueada
})


@dataclass
class CopyResult:
    """Resultado de una operación open/close/modify en un follower."""

    success: bool
    retryable: bool = False
    retcode: int | None = None
    detail: str = ""
    follower_lot: float | None = None
    follower_ticket: int | None = None
    already_exists: bool = False

    @classmethod
    def ok(
        cls,
        *,
        follower_lot: float | None = None,
        follower_ticket: int | None = None,
        already_exists: bool = False,
        detail: str = "",
        retcode: int | None = None,
    ) -> CopyResult:
        return cls(
            success=True,
            retryable=False,
            retcode=retcode,
            detail=detail,
            follower_lot=follower_lot,
            follower_ticket=follower_ticket,
            already_exists=already_exists,
        )

    @classmethod
    def fail(
        cls,
        detail: str,
        *,
        retryable: bool = False,
        retcode: int | None = None,
    ) -> CopyResult:
        return cls(
            success=False,
            retryable=retryable,
            retcode=retcode,
            detail=detail,
        )

    @classmethod
    def from_retcode(cls, retcode: int | None, detail: str = "") -> CopyResult:
        if retcode is None:
            return cls.fail(detail or "sin respuesta del terminal", retryable=True)
        retryable = retcode in RETRYABLE_RETCODES
        return cls.fail(detail or f"retcode={retcode}", retryable=retryable, retcode=retcode)


@dataclass
class CopyAck:
    """Confirmación de un worker hacia el proceso main."""

    event_id: str
    follower: str
    action: str
    master_ticket: int
    success: bool
    retryable: bool = False
    retcode: int | None = None
    detail: str = ""
    follower_lot: float | None = None
    follower_ticket: int | None = None
    attempt: int = 1

    @classmethod
    def from_result(
        cls,
        *,
        event_id: str,
        follower: str,
        action: str,
        master_ticket: int,
        result: CopyResult,
        attempt: int = 1,
    ) -> CopyAck:
        return cls(
            event_id=event_id,
            follower=follower,
            action=action,
            master_ticket=master_ticket,
            success=result.success,
            retryable=result.retryable,
            retcode=result.retcode,
            detail=result.detail,
            follower_lot=result.follower_lot,
            follower_ticket=result.follower_ticket,
            attempt=attempt,
        )


AckStatus = str  # pending | ok | fail_retry | fail_final


@dataclass
class FollowerAckState:
    status: AckStatus = "pending"
    attempt: int = 0
    retcode: int | None = None
    detail: str = ""
    next_retry_at: float = 0.0  # monotonic time
    last_update: float = 0.0
