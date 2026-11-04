"""Broker abstraction.

There is deliberately NO order-placement method here: this codebase never sends real orders.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.enums import CancelReason


class InstrumentNotFoundError(Exception):
    cancel_reason = CancelReason.INSTRUMENT_NOT_FOUND


class BrokerAuthError(Exception):
    """The broker rejected our credentials (e.g. the daily access token expired)."""


@dataclass(frozen=True)
class Instrument:
    instrument_key: str
    trading_symbol: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int


@dataclass(frozen=True)
class Candle:
    time: datetime  # candle start, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


LtpCallback = Callable[[str, float, datetime], Awaitable[None]]


class BrokerClient(Protocol):
    async def resolve_instrument(
        self, underlying: str, expiry: str | None, strike: float, option_type: str
    ) -> Instrument: ...

    async def get_ltp(self, instrument_key: str) -> float: ...

    async def get_ltps(self, instrument_keys: list[str]) -> dict[str, float]: ...

    async def get_historical_candles(
        self, instrument_key: str, interval: str, start: datetime, end: datetime
    ) -> list[Candle]: ...

    async def stream_ltp(self, instrument_keys: list[str], callback: LtpCallback) -> None: ...
