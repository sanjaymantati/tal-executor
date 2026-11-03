from datetime import datetime, timedelta, timezone

from app.ui.pnl import ClosedTrade, build_daily_pnl, build_equity_curve, summarise_trades

DAY_ONE = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


def closed_trade(trade_id: int, pnl: float, exit_time: datetime, fees: float = 0.0) -> ClosedTrade:
    return ClosedTrade(
        trade_id=trade_id,
        label=f"#{trade_id} NIFTY 24500 CE",
        qty=75,
        entry_price=100,
        stop_loss=90,
        exit_price=100 + pnl / 75,
        entry_time=exit_time - timedelta(minutes=5),
        exit_time=exit_time,
        exit_reason="RR_TARGET" if pnl > 0 else "SL_HIT",
        pnl=pnl,
        r_multiple=pnl / 750,
        fees=fees,
    )


TRADES = [
    closed_trade(1, 600, DAY_ONE, fees=40),
    closed_trade(2, -300, DAY_ONE + timedelta(hours=1), fees=40),
    closed_trade(3, 150, DAY_ONE + timedelta(days=1), fees=40),
]


def test_summary_counts_wins_losses_and_ratios():
    summary = summarise_trades(TRADES)

    assert summary["closed_trades"] == 3
    assert (summary["wins"], summary["losses"]) == (2, 1)
    assert summary["win_rate"] == 2 / 3
    assert summary["net_pnl"] == 450
    assert summary["gross_pnl"] == 570
    assert summary["fees"] == 120
    assert summary["average_win"] == 375 and summary["average_loss"] == -300
    assert (summary["best_trade"], summary["worst_trade"]) == (600, -300)
    assert summary["profit_factor"] == 750 / 300


def test_summary_reports_stop_loss_exits_and_risk():
    summary = summarise_trades(TRADES)

    assert summary["stop_loss_hits"] == 1
    assert summary["stop_loss_pnl"] == -300
    assert summary["total_risk"] == 3 * (100 - 90) * 75


def test_risk_amount_is_the_loss_at_stop_loss():
    assert TRADES[0].risk_amount == 750


def test_summary_without_trades_has_no_ratios():
    summary = summarise_trades([])

    assert summary["closed_trades"] == 0 and summary["net_pnl"] == 0
    assert summary["win_rate"] is None and summary["profit_factor"] is None


def test_equity_curve_accumulates_in_exit_order():
    curve = build_equity_curve(TRADES)

    assert [point["cumulative_pnl"] for point in curve] == [600, 300, 450]
    assert [point["trade_id"] for point in curve] == [1, 2, 3]


def test_daily_pnl_groups_by_ist_day():
    daily = build_daily_pnl(TRADES)

    assert daily == [
        {"date": "2026-11-02", "trades": 2, "pnl": 300},
        {"date": "2026-11-03", "trades": 1, "pnl": 150},
    ]
