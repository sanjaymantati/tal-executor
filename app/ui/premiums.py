import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.broker.base import BrokerClient
from app.db.models import PaperTrade
from app.enums import PaperTradeStatus
from app.execution.machine import ORDERS_PER_ROUND_TRIP
from app.execution.types import TradingCosts
from app.timeutil import IST

log = structlog.get_logger(__name__)

PREMIUM_FETCH_TIMEOUT_SEC = 5.0
ACTIVE_PAPER_STATUSES = (PaperTradeStatus.PENDING.value, PaperTradeStatus.OPEN.value)
HTTP_NOT_FETCHED = 404
HTTP_TOO_MANY_REQUESTS = 429


@dataclass(frozen=True)
class PremiumQuotes:
    """Prices by instrument key; `missing_code` is the HTTP code shown for a key without a price."""

    prices: dict[str, float]
    missing_code: int

    def find_price(self, instrument_key: str | None) -> float | None:
        return self.prices.get(instrument_key)


class LivePriceQuotes:
    """Reads current option premiums for the dashboard. A failure only means 'no price yet'."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker

    async def find_premiums(self, instrument_keys: list[str]) -> PremiumQuotes:
        if not instrument_keys:
            return PremiumQuotes({}, HTTP_NOT_FETCHED)
        try:
            prices = await asyncio.wait_for(
                self._broker.get_ltps(instrument_keys), PREMIUM_FETCH_TIMEOUT_SEC
            )
        except Exception as error:
            log.warning("premium_fetch_failed", error=repr(error))
            return PremiumQuotes({}, find_missing_code(error))
        return PremiumQuotes(prices, HTTP_NOT_FETCHED)


def find_missing_code(error: Exception) -> int:
    if is_rate_limited(error):
        return HTTP_TOO_MANY_REQUESTS
    return HTTP_NOT_FETCHED


def is_rate_limited(error: Exception) -> bool:
    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code == HTTP_TOO_MANY_REQUESTS
    )


def is_active_paper_trade(paper: PaperTrade) -> bool:
    return paper.status in ACTIVE_PAPER_STATUSES


def is_from_earlier_session(created_at: datetime, now: datetime) -> bool:
    return created_at.astimezone(IST).date() < now.astimezone(IST).date()


def describe_live_values(
    paper: PaperTrade, quotes: PremiumQuotes, instrument_key: str | None, costs: TradingCosts
) -> dict[str, Any]:
    """Premium is only live for an active paper trade; P&L is unrealised while open, realised once closed."""
    premium = quotes.find_price(instrument_key)
    active = is_active_paper_trade(paper)
    return {
        "current_premium": premium if active else None,
        "premium_error_code": quotes.missing_code if active and premium is None else None,
        "live_pnl": calculate_live_pnl(paper, premium, costs),
    }


def calculate_live_pnl(
    paper: PaperTrade, premium: float | None, costs: TradingCosts
) -> float | None:
    if paper.status == PaperTradeStatus.CLOSED.value:
        return paper.pnl
    if paper.status != PaperTradeStatus.OPEN.value or premium is None:
        return None
    return calculate_unrealised_pnl(paper, premium, costs)


def calculate_unrealised_pnl(paper: PaperTrade, premium: float, costs: TradingCosts) -> float:
    """What closing now would book; mirrors PaperPositionMachine._close."""
    exit_price = premium - costs.slippage
    fees = costs.brokerage_per_order * ORDERS_PER_ROUND_TRIP
    return (exit_price - paper.entry_price) * paper.qty - fees
