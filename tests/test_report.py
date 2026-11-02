from app.db.models import PaperTrade, Trade
from app.enums import PaperTradeStatus, TradeState
from app.report import ReportBuilder, format_report


async def test_report_summarises_trades_and_paper_results(session_factory):
    async with session_factory.begin() as session:
        trades = [
            Trade(state=TradeState.CLOSED.value),
            Trade(state=TradeState.CLOSED.value),
            Trade(state=TradeState.CANCELLED.value),
            Trade(state=TradeState.EXPIRED_INCOMPLETE.value),
            Trade(state=TradeState.PARTIAL.value),
        ]
        session.add_all(trades)
        await session.flush()
        session.add_all(
            [
                PaperTrade(trade_id=trades[0].id, status=PaperTradeStatus.CLOSED.value, pnl=1500.0, r_multiple=2.0),
                PaperTrade(trade_id=trades[1].id, status=PaperTradeStatus.CLOSED.value, pnl=-700.0, r_multiple=-1.0),
                PaperTrade(trade_id=trades[2].id, status=PaperTradeStatus.CANCELLED.value),
            ]
        )

    report = await ReportBuilder(session_factory).build_report()

    assert report.trades_captured == 5
    assert report.incomplete_trades == 1
    assert report.closed_paper_trades == 2
    assert report.cancelled_paper_trades == 1
    assert report.win_rate == 0.5
    assert report.total_pnl == 800.0
    assert report.average_r_multiple == 0.5
    text = format_report(report)
    assert "Win rate:               50.0%" in text
    assert "Total P&L:              800.00" in text


async def test_report_on_an_empty_database_does_not_fail(session_factory):
    report = await ReportBuilder(session_factory).build_report()

    assert report.trades_captured == 0
    assert report.win_rate is None
    assert "n/a" in format_report(report)
