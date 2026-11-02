from datetime import datetime, time, timedelta, timezone

import pytest

from app.config import ExecutionConfig
from app.enums import (
    CancelReason,
    EventType,
    ExitMode,
    ExitReason,
    PaperTradeStatus,
)
from app.execution.exit_strategies import RiskRewardExit, build_exit_strategy
from app.execution.machine import PaperPositionMachine
from app.execution.types import PriceBar, TradePlan, TradingCosts

START = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
MARKET_CLOSE = datetime(2026, 11, 2, 9, 45, tzinfo=timezone.utc)  # 15:15 IST
QUANTITY = 30
NO_COSTS = TradingCosts(slippage=0, brokerage_per_order=0)


def make_plan() -> TradePlan:
    return TradePlan(
        entry_low=140,
        entry_high=145,
        stop_loss=120,
        targets=(170, 190),
        quantity=QUANTITY,
        entry_timeout_at=START + timedelta(minutes=60),
        market_close_at=MARKET_CLOSE,
    )


def make_machine(costs: TradingCosts = NO_COSTS, plan: TradePlan | None = None) -> PaperPositionMachine:
    return PaperPositionMachine(plan or make_plan(), RiskRewardExit(2, 1), costs)


def bar(minute: int, open: float, high: float, low: float, close: float) -> PriceBar:
    return PriceBar(START + timedelta(minutes=minute), open, high, low, close)


def entry_bar(minute: int = 1) -> PriceBar:
    return bar(minute, 142, 143, 141, 142)  # fills at 142 -> risk 22 -> 2R target 186


def event_types(events) -> list[EventType]:
    return [event.event_type for event in events]


def test_stop_loss_exit_is_checked_every_bar_after_entry():
    machine = make_machine()
    assert event_types(machine.on_bar(entry_bar())) == [EventType.ENTRY_TRIGGERED]

    events = machine.on_bar(bar(2, 141, 142, 119, 120))

    assert event_types(events) == [EventType.SL_HIT]
    assert machine.status is PaperTradeStatus.CLOSED
    assert machine.result.exit_price == 120
    assert machine.result.exit_reason is ExitReason.SL_HIT
    assert machine.result.pnl == (120 - 142) * QUANTITY
    assert machine.result.r_multiple == pytest.approx(-1.0)


def test_two_r_target_exit_with_channel_levels_recorded_as_information():
    machine = make_machine()
    machine.on_bar(entry_bar())

    events = machine.on_bar(bar(2, 150, 190, 149, 188))

    assert event_types(events) == [
        EventType.TARGET_LEVEL_TOUCHED,
        EventType.TARGET_LEVEL_TOUCHED,
        EventType.RR_TARGET_HIT,
    ]
    assert [e.payload.get("level") for e in events[:2]] == ["T1", "T2"]
    assert machine.result.exit_price == pytest.approx(186)  # entry + 2 x (142 - 120)
    assert machine.result.exit_reason is ExitReason.RR_TARGET
    assert machine.result.r_multiple == pytest.approx(2.0)
    assert machine.result.pnl == pytest.approx((186 - 142) * QUANTITY)


def test_channel_levels_do_not_close_the_trade():
    machine = make_machine()
    machine.on_bar(entry_bar())

    events = machine.on_bar(bar(2, 150, 171, 149, 170))

    assert event_types(events) == [EventType.TARGET_LEVEL_TOUCHED]
    assert machine.status is PaperTradeStatus.OPEN


def test_each_channel_level_is_reported_once():
    machine = make_machine()
    machine.on_bar(entry_bar())
    machine.on_bar(bar(2, 150, 171, 149, 170))

    assert machine.on_bar(bar(3, 160, 172, 150, 160)) == []


def test_stop_loss_wins_when_one_candle_touches_both_stop_and_target():
    machine = make_machine()
    machine.on_bar(entry_bar())

    events = machine.on_bar(bar(2, 142, 200, 118, 150))

    assert event_types(events) == [EventType.SL_HIT]
    assert machine.result.exit_reason is ExitReason.SL_HIT


def test_target_is_not_credited_on_the_entry_candle():
    machine = make_machine()

    events = machine.on_bar(bar(1, 142, 200, 141, 190))

    assert event_types(events) == [EventType.ENTRY_TRIGGERED]
    assert machine.status is PaperTradeStatus.OPEN
    assert event_types(machine.on_bar(bar(2, 190, 190, 189, 190)))[-1] is EventType.RR_TARGET_HIT


def test_stop_loss_on_the_entry_candle_is_taken_conservatively():
    machine = make_machine()

    events = machine.on_bar(bar(1, 150, 150, 118, 130))

    assert event_types(events) == [EventType.ENTRY_TRIGGERED, EventType.SL_HIT]
    assert events[0].price == 145  # price came down to the top of the entry range


def test_entry_price_is_clamped_into_the_range():
    below = make_machine()
    below.on_bar(bar(1, 130, 141, 129, 141))
    assert below.position.entry_price == 140

    inside = make_machine()
    inside.on_bar(bar(1, 143, 144, 142, 143))
    assert inside.position.entry_price == 143


def test_end_of_day_exit_at_market_close_bar():
    machine = make_machine()
    machine.on_bar(entry_bar())
    machine.on_bar(bar(2, 150, 160, 140, 150))

    events = machine.on_bar(PriceBar(MARKET_CLOSE, 155, 160, 150, 158))

    assert event_types(events) == [EventType.EOD_EXIT]
    assert machine.result.exit_price == 155
    assert machine.result.exit_reason is ExitReason.EOD


def test_end_of_day_exit_when_data_stops_before_close():
    machine = make_machine()
    machine.on_bar(entry_bar())

    events = machine.end_session(price=148, time=MARKET_CLOSE)

    assert event_types(events) == [EventType.EOD_EXIT]
    assert machine.result.exit_price == 148


def test_entry_never_triggered_times_out():
    machine = make_machine()
    assert machine.on_bar(bar(1, 160, 165, 155, 160)) == []
    assert machine.on_bar(bar(59, 160, 165, 155, 160)) == []

    events = machine.on_bar(bar(60, 160, 165, 155, 160))

    assert event_types(events) == [EventType.CANCELLED]
    assert machine.status is PaperTradeStatus.CANCELLED
    assert machine.cancel_reason is CancelReason.ENTRY_TIMEOUT
    assert machine.position is None


def test_entry_never_triggered_cancels_at_market_close_if_before_timeout():
    plan = TradePlan(140, 145, 120, (170,), QUANTITY, MARKET_CLOSE + timedelta(hours=1), MARKET_CLOSE)
    machine = make_machine(plan=plan)

    machine.on_bar(PriceBar(MARKET_CLOSE, 160, 165, 155, 160))

    assert machine.cancel_reason is CancelReason.MARKET_CLOSE


def test_stop_loss_hit_before_entry_cancels_the_trade():
    machine = make_machine()

    events = machine.on_bar(bar(1, 125, 130, 118, 120))

    assert event_types(events) == [EventType.CANCELLED]
    assert machine.cancel_reason is CancelReason.SL_BEFORE_ENTRY


def test_entry_range_at_or_below_stop_loss_is_invalid():
    plan = TradePlan(120, 125, 120, (170,), QUANTITY, START + timedelta(hours=1), MARKET_CLOSE)
    machine = make_machine(plan=plan)

    machine.on_bar(bar(1, 122, 123, 121, 122))

    assert machine.cancel_reason is CancelReason.INVALID_LEVELS


def test_live_ticks_use_the_same_rules():
    machine = make_machine()
    tick = lambda minute, price: PriceBar.from_tick(START + timedelta(minutes=minute), price)

    assert machine.on_bar(tick(1, 150)) == []
    assert event_types(machine.on_bar(tick(2, 144))) == [EventType.ENTRY_TRIGGERED]
    assert machine.on_bar(tick(3, 160)) == []
    assert event_types(machine.on_bar(tick(4, 120))) == [EventType.SL_HIT]
    assert machine.result.exit_price == 120


def test_finished_machine_ignores_further_bars():
    machine = make_machine()
    machine.on_bar(entry_bar())
    machine.on_bar(bar(2, 141, 142, 119, 120))

    assert machine.on_bar(bar(3, 100, 300, 50, 100)) == []


def test_slippage_and_brokerage_reduce_pnl():
    machine = make_machine(TradingCosts(slippage=1, brokerage_per_order=20))
    machine.on_bar(entry_bar())
    assert machine.position.entry_price == 143

    machine.on_bar(bar(2, 141, 142, 119, 120))

    assert machine.result.exit_price == 119
    assert machine.result.fees == 40
    assert machine.result.pnl == (119 - 143) * QUANTITY - 40
    assert machine.result.r_multiple == pytest.approx((119 - 143) / (143 - 120))


def test_restored_position_keeps_its_progress():
    machine = make_machine()
    machine.restore_open_position(142, START, touched_levels={1})

    events = machine.on_bar(bar(5, 160, 175, 150, 170))

    assert events == []  # T1 already reported, T2 not reached


def test_only_the_risk_reward_exit_mode_is_implemented():
    config = ExecutionConfig(
        lots=1, lot_size_map={"NIFTY": 75}, rr_reward=2, rr_risk=1, exit_mode=ExitMode.RR,
        entry_timeout_min=60, market_close_exit=time(15, 15), slippage=0, brokerage_per_order=0,
    )
    assert isinstance(build_exit_strategy(config), RiskRewardExit)
    for unsupported in (ExitMode.SIGNAL_TARGETS, ExitMode.HYBRID):
        with pytest.raises(NotImplementedError):
            build_exit_strategy(config.model_copy(update={"exit_mode": unsupported}))


def test_reward_to_risk_is_configurable():
    machine = PaperPositionMachine(make_plan(), RiskRewardExit(3, 1), NO_COSTS)
    machine.on_bar(entry_bar())

    machine.on_bar(bar(2, 150, 190, 149, 188))
    assert machine.status is PaperTradeStatus.OPEN  # 3R target is 208

    machine.on_bar(bar(3, 190, 210, 189, 205))
    assert machine.result.exit_price == pytest.approx(208)
