from dataclasses import dataclass


@dataclass
class Position:
    ticket: int
    symbol: str
    order_type: int      # 0=BUY, 1=SELL
    volume: float
    open_price: float
    sl: float
    tp: float
    comment: str

    @property
    def is_buy(self) -> bool:
        return self.order_type == 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return False
        return (
            self.ticket == other.ticket
            and self.volume == other.volume
            and self.sl == other.sl
            and self.tp == other.tp
        )

    def __hash__(self) -> int:
        return hash(self.ticket)
