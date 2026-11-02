from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.enums import EventType, ExitReason


@dataclass(frozen=True)
class PriceBar:
    """A price observation window. A live tick is a bar with open == high == low == close."""

    time: datetime
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_tick(cls, time: datetime, price: float) -> "PriceBar":
        return cls(time=time, open=price, high=price, low=price, close=price)


@dataclass(frozen=True)
class TradePlan:
    entry_low: float
    entry_high: float
    stop_loss: float
    targets: tuple[float, ...]
    quantity: int
    entry_timeout_at: datetime
    market_close_at: datetime


@dataclass(frozen=True)
class TradingCosts:
    slippage: float
    brokerage_per_order: float


@dataclass
class OpenPosition:
    entry_price: float
    entry_time: datetime
    stop_loss: float
    targets: tuple[float, ...]
    touched_levels: set[int] = field(default_factory=set)

    @property
    def risk(self) -> float:
        return self.entry_price - self.stop_loss


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    price: float


@dataclass(frozen=True)
class MachineEvent:
    event_type: EventType
    price: float | None
    time: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class TradeResult:
    exit_price: float
    exit_time: datetime
    exit_reason: ExitReason
    pnl: float
    r_multiple: float
    fees: float
