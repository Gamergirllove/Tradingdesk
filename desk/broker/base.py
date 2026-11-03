from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Ack:
    ok: bool
    order_id: str | None = None
    client_order_id: str | None = None
    status: str = ""             # resting | filled | partial | rejected | canceled
    filled: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    message: str = ""


@dataclass
class Pos:
    qty: float = 0.0            # + YES, - NO
    avg: float = 0.0            # YES-price terms
    realized: float = 0.0
    fees: float = 0.0
    extra: dict = field(default_factory=dict)

    def exposure(self) -> float:
        if self.qty > 0:
            return self.qty * self.avg
        if self.qty < 0:
            return -self.qty * (1 - self.avg)
        return 0.0
