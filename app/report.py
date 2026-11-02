"""CLI report: `python -m app.report`."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import func, select

from app.config import DatabaseSettings
from app.db.models import PaperTrade, Trade
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.enums import PaperTradeStatus, TradeState


@dataclass(frozen=True)
class Report:
    trades_captured: int
    trades_by_state: dict[str, int]
    incomplete_trades: int
    cancelled_paper_trades: int
    closed_paper_trades: int
    winning_paper_trades: int
    total_pnl: float
    average_r_multiple: float | None

    @property
    def win_rate(self) -> float | None:
        if self.closed_paper_trades == 0:
            return None
        return self.winning_paper_trades / self.closed_paper_trades


class ReportBuilder:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def build_report(self) -> Report:
        async with self._session_factory() as session:
            trades_by_state = await self._count_trades_by_state(session)
            closed = PaperTrade.status == PaperTradeStatus.CLOSED.value
            totals = await session.execute(
                select(
                    func.count(PaperTrade.id),
                    func.coalesce(func.sum(PaperTrade.pnl), 0.0),
                    func.avg(PaperTrade.r_multiple),
                ).where(closed)
            )
            closed_count, total_pnl, average_r = totals.one()
            winners = await session.execute(
                select(func.count(PaperTrade.id)).where(closed, PaperTrade.pnl > 0)
            )
            cancelled = await session.execute(
                select(func.count(PaperTrade.id)).where(
                    PaperTrade.status == PaperTradeStatus.CANCELLED.value
                )
            )
        return Report(
            trades_captured=sum(trades_by_state.values()),
            trades_by_state=trades_by_state,
            incomplete_trades=trades_by_state.get(TradeState.EXPIRED_INCOMPLETE.value, 0),
            cancelled_paper_trades=cancelled.scalar_one(),
            closed_paper_trades=closed_count,
            winning_paper_trades=winners.scalar_one(),
            total_pnl=float(total_pnl),
            average_r_multiple=float(average_r) if average_r is not None else None,
        )

    async def _count_trades_by_state(self, session) -> dict[str, int]:
        result = await session.execute(select(Trade.state, func.count(Trade.id)).group_by(Trade.state))
        return {state: count for state, count in result.all()}


def format_report(report: Report) -> str:
    win_rate = f"{report.win_rate:.1%}" if report.win_rate is not None else "n/a"
    average_r = f"{report.average_r_multiple:.2f}" if report.average_r_multiple is not None else "n/a"
    state_lines = [f"    {state}: {count}" for state, count in sorted(report.trades_by_state.items())]
    lines = [
        f"Trades captured:        {report.trades_captured}",
        *state_lines,
        f"Incomplete (expired):   {report.incomplete_trades}",
        f"Paper trades closed:    {report.closed_paper_trades}",
        f"Paper trades cancelled: {report.cancelled_paper_trades}",
        f"Win rate:               {win_rate}",
        f"Total P&L:              {report.total_pnl:.2f}",
        f"Average R:              {average_r}",
    ]
    return "\n".join(lines)


async def print_report() -> None:
    settings = DatabaseSettings()
    engine = create_engine(settings.database_url)
    try:
        report = await ReportBuilder(create_session_factory(engine)).build_report()
        print(format_report(report))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(print_report())
