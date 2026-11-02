from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.db.models import PaperTrade, Trade
from app.db.session import SessionFactory
from app.enums import PaperTradeStatus
from app.timeutil import to_ist
from app.ui.queries import label_trade

MAX_TRADES_LISTED = 200


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: int
    label: str
    qty: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    pnl: float
    r_multiple: float
    fees: float


class PnlQueries:
    """Read-only profit and loss figures built from closed paper trades."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def build_pnl_view(self) -> dict[str, Any]:
        closed_trades = await self._load_closed_trades()
        return {
            "summary": summarise_trades(closed_trades),
            "open_positions": await self._count_open_positions(),
            "equity_curve": build_equity_curve(closed_trades),
            "daily": build_daily_pnl(closed_trades),
            "trades": [describe_closed_trade(t) for t in reversed(closed_trades)][:MAX_TRADES_LISTED],
        }

    async def _load_closed_trades(self) -> list[ClosedTrade]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PaperTrade, Trade)
                .join(Trade, Trade.id == PaperTrade.trade_id)
                .where(
                    PaperTrade.status == PaperTradeStatus.CLOSED.value,
                    PaperTrade.exit_time.is_not(None),
                    PaperTrade.pnl.is_not(None),
                )
                .order_by(PaperTrade.exit_time, PaperTrade.id)
            )
            return [to_closed_trade(paper, trade) for paper, trade in result.all()]

    async def _count_open_positions(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count(PaperTrade.id)).where(
                    PaperTrade.status == PaperTradeStatus.OPEN.value
                )
            )
            return result.scalar_one()


def to_closed_trade(paper: PaperTrade, trade: Trade) -> ClosedTrade:
    return ClosedTrade(
        trade_id=trade.id,
        label=label_trade(trade),
        qty=paper.qty,
        entry_price=paper.entry_price,
        exit_price=paper.exit_price,
        entry_time=paper.entry_time,
        exit_time=paper.exit_time,
        exit_reason=paper.exit_reason,
        pnl=paper.pnl,
        r_multiple=paper.r_multiple,
        fees=paper.fees or 0.0,
    )


def summarise_trades(trades: list[ClosedTrade]) -> dict[str, Any]:
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    net = sum(t.pnl for t in trades)
    fees = sum(t.fees for t in trades)
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else None,
        "net_pnl": net,
        "gross_pnl": net + fees,
        "fees": fees,
        "average_win": sum(wins) / len(wins) if wins else None,
        "average_loss": sum(losses) / len(losses) if losses else None,
        "best_trade": max((t.pnl for t in trades), default=None),
        "worst_trade": min((t.pnl for t in trades), default=None),
        "average_r_multiple": sum(t.r_multiple for t in trades) / len(trades) if trades else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
    }


def build_equity_curve(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    curve = []
    running_total = 0.0
    for trade in trades:
        running_total += trade.pnl
        curve.append(
            {"time": trade.exit_time, "trade_id": trade.trade_id, "cumulative_pnl": running_total}
        )
    return curve


def build_daily_pnl(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    """One row per IST trading day, oldest first."""
    days: dict[str, dict[str, Any]] = {}
    for trade in trades:
        day = days.setdefault(
            to_ist(trade.exit_time).date().isoformat(), {"trades": 0, "pnl": 0.0}
        )
        day["trades"] += 1
        day["pnl"] += trade.pnl
    return [{"date": date, **figures} for date, figures in days.items()]


def describe_closed_trade(trade: ClosedTrade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "label": trade.label,
        "qty": trade.qty,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "r_multiple": trade.r_multiple,
        "fees": trade.fees,
    }
