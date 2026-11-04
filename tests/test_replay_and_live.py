import asyncio
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select

from app.broker.base import Candle, Instrument, InstrumentNotFoundError
from app.config import ExecutionConfig
from app.db.models import ConfigSnapshot, PaperTrade, RawMessage, Trade, TradeEvent, TradeMessage
from app.enums import (
    EventType,
    StopRequestResult,
    ExitMode,
    PaperTradeStatus,
    TradeMessageRole,
    TradeState,
)
from app.execution.executor import HERO_ZERO_MESSAGE, PaperTradeExecutor, is_stated_same_day_expiry
from app.execution.ledger import PaperTradeLedger
from app.execution.live import LiveTradeWatcher
from app.execution.replay import TradeReplayer
from tests.conftest import BASE_TIME, CHANNEL_ID

LOT_SIZE = 30
INSTRUMENT = Instrument("NSE_FO|1", "BANKNIFTY", "BANKNIFTY", date(2026, 11, 26), 52000, "CE", LOT_SIZE)


utc_clock = lambda: BASE_TIME + timedelta(minutes=10)


def make_config(**overrides) -> ExecutionConfig:
    values = dict(
        lots=2,
        lot_size_map={"BANKNIFTY": LOT_SIZE},
        rr_reward=2,
        rr_risk=1,
        exit_mode=ExitMode.RR,
        entry_timeout_min=60,
        market_close_exit=time(15, 15),
        slippage=0,
        brokerage_per_order=0,
    )
    return ExecutionConfig(**{**values, **overrides})


def http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://broker.test/ltp")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status_code, request=request)
    )


class FakeBroker:
    def __init__(self, candles: list[Candle], ticks: list[float]) -> None:
        self.candles = candles
        self.ticks = ticks
        self.resolve_calls = 0
        self.fail_resolution = False
        self.fail_ltp = False
        self.ltp_missing = False
        self.ltp_calls = 0
        self.ltps_calls = 0
        self.stream_calls = 0
        self.ltp_http_status: int | None = None
        self.instrument = INSTRUMENT

    async def resolve_instrument(self, underlying, expiry, strike, option_type) -> Instrument:
        self.resolve_calls += 1
        if self.fail_resolution:
            raise InstrumentNotFoundError("nope")
        return self.instrument

    async def get_ltp(self, instrument_key: str) -> float:
        self.ltp_calls += 1
        if self.ltp_missing:
            raise InstrumentNotFoundError(f"no LTP returned for {instrument_key}")
        return self.ticks[0]

    async def get_ltps(self, instrument_keys) -> dict[str, float]:
        self.ltps_calls += 1
        if self.fail_ltp:
            raise RuntimeError("broker down")
        if self.ltp_http_status is not None:
            raise http_status_error(self.ltp_http_status)
        return {key: self.ticks[0] for key in instrument_keys} if self.ticks else {}

    async def get_historical_candles(self, instrument_key, interval, start, end) -> list[Candle]:
        return [c for c in self.candles if start <= c.time <= end]

    async def stream_ltp(self, instrument_keys, callback) -> None:
        self.stream_calls += 1
        base = BASE_TIME + timedelta(minutes=5)
        for index, price in enumerate(self.ticks):
            await callback(instrument_keys[0], price, base + timedelta(seconds=index))
        await asyncio.sleep(3600)


def candle(minute: int, open: float, high: float, low: float, close: float) -> Candle:
    return Candle(BASE_TIME + timedelta(minutes=minute), open, high, low, close, 1000)


async def create_complete_trade(
    session_factory, lot_underlying: str = "BANKNIFTY", expiry: str | None = None
) -> int:
    async with session_factory.begin() as session:
        message = RawMessage(
            channel_id=CHANNEL_ID,
            tg_message_id=1,
            text="signal",
            sent_at=BASE_TIME,
            raw_json=None,
        )
        session.add(message)
        trade = Trade(
            underlying=lot_underlying,
            expiry=expiry,
            strike=52000,
            option_type="CE",
            entry_low=140,
            entry_high=145,
            stop_loss=120,
            targets_json=[170, 190],
            state=TradeState.COMPLETE.value,
        )
        session.add(trade)
        await session.flush()
        session.add(
            TradeMessage(trade_id=trade.id, message_id=message.id, role=TradeMessageRole.ORIGIN.value)
        )
        return trade.id


def build_executor(session_factory, broker, config=None) -> PaperTradeExecutor:
    clock = lambda: BASE_TIME + timedelta(minutes=1)
    return PaperTradeExecutor(PaperTradeLedger(session_factory), broker, config or make_config(), clock)


async def load_one(session_factory, model):
    async with session_factory() as session:
        return (await session.execute(select(model))).scalar_one()


async def load_event_types(session_factory) -> list[str]:
    async with session_factory() as session:
        rows = await session.execute(select(TradeEvent.event_type).order_by(TradeEvent.id))
        return list(rows.scalars())


async def test_replay_runs_a_stored_trade_over_candles(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(
        candles=[
            candle(0, 150, 151, 148, 150),  # starts before the signal's next minute boundary
            candle(1, 150, 152, 146, 147),
            candle(2, 147, 147, 143, 144),  # price comes down into range: fills at 145
            candle(3, 144, 175, 140, 170),  # T1 (170) touched
            candle(4, 170, 200, 160, 190),  # T2 (190) touched; 2R target = 145 + 2 x 25 = 195
        ],
        ticks=[],
    )
    replayer = TradeReplayer(build_executor(session_factory, broker), broker)

    assert await replayer.replay_trade(trade_id) is True

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CLOSED.value
    assert paper.qty == 2 * LOT_SIZE
    assert paper.entry_price == 145
    assert paper.exit_reason == "RR_TARGET"
    assert paper.exit_price == 195
    assert paper.pnl == (195 - 145) * 2 * LOT_SIZE
    assert paper.r_multiple == 2.0
    assert await load_event_types(session_factory) == [
        EventType.ENTRY_TRIGGERED.value,
        EventType.TARGET_LEVEL_TOUCHED.value,
        EventType.TARGET_LEVEL_TOUCHED.value,
        EventType.RR_TARGET_HIT.value,
    ]
    trade = await load_one(session_factory, Trade)
    assert trade.state == TradeState.CLOSED.value
    assert trade.instrument_key == "NSE_FO|1"
    snapshot = await load_one(session_factory, ConfigSnapshot)
    assert snapshot.config_json["lots"] == 2


async def test_replaying_twice_does_not_create_a_second_paper_trade(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[candle(2, 143, 144, 141, 143)], ticks=[])
    replayer = TradeReplayer(build_executor(session_factory, broker), broker)

    assert await replayer.replay_trade(trade_id) is True
    assert await replayer.replay_trade(trade_id) is False

    async with session_factory() as session:
        assert len((await session.execute(select(PaperTrade))).scalars().all()) == 1


async def test_replay_without_entry_is_cancelled_and_logged(session_factory):
    trade_id = await create_complete_trade(session_factory)
    candles = [candle(m, 160, 165, 155, 160) for m in range(1, 90)]
    broker = FakeBroker(candles=candles, ticks=[])
    replayer = TradeReplayer(build_executor(session_factory, broker), broker)

    await replayer.replay_trade(trade_id)

    paper = await load_one(session_factory, PaperTrade)
    trade = await load_one(session_factory, Trade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    assert trade.state == TradeState.CANCELLED.value
    assert await load_event_types(session_factory) == [EventType.CANCELLED.value]


async def test_replay_with_no_candles_is_cancelled(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[])

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CANCELLED.value


async def test_open_position_is_closed_at_end_of_day_when_candles_run_out(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(
        candles=[candle(1, 143, 144, 141, 143), candle(2, 143, 150, 140, 150)], ticks=[]
    )

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    paper = await load_one(session_factory, PaperTrade)
    assert paper.exit_reason == "EOD"
    assert paper.exit_price == 150


async def test_unresolvable_instrument_cancels_the_paper_trade(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[])
    broker.fail_resolution = True

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    async with session_factory() as session:
        event = (await session.execute(select(TradeEvent))).scalar_one()
    assert event.payload_json == {"reason": "INSTRUMENT_NOT_FOUND"}


def test_stated_expiry_phrases_and_dates_mean_today():
    today = date(2026, 11, 2)
    assert is_stated_same_day_expiry("today", today)
    assert is_stated_same_day_expiry("hero-zero", today)
    assert is_stated_same_day_expiry("2026-11-02", today)
    assert not is_stated_same_day_expiry("2026-11-26", today)
    assert not is_stated_same_day_expiry(None, today)


async def test_same_day_contract_is_cancelled_as_hero_zero(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[])
    broker.instrument = Instrument(
        "NSE_FO|1", "BANKNIFTY", "BANKNIFTY", date(2026, 11, 2), 52000, "CE", LOT_SIZE
    )

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    paper = await load_one(session_factory, PaperTrade)
    trade = await load_one(session_factory, Trade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    assert paper.entry_price is None
    assert trade.state == TradeState.CANCELLED.value
    async with session_factory() as session:
        event = (await session.execute(select(TradeEvent))).scalar_one()
    assert event.payload_json == {"reason": "HERO_ZERO", "message": HERO_ZERO_MESSAGE}


async def test_stated_expiry_today_cancels_without_resolving(session_factory):
    trade_id = await create_complete_trade(session_factory, expiry="today")
    broker = FakeBroker(candles=[], ticks=[])

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    assert broker.resolve_calls == 0
    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    assert paper.entry_price is None
    async with session_factory() as session:
        event = (await session.execute(select(TradeEvent))).scalar_one()
    assert event.payload_json == {"reason": "HERO_ZERO", "message": HERO_ZERO_MESSAGE}


async def test_unknown_lot_size_cancels_the_paper_trade(session_factory):
    trade_id = await create_complete_trade(session_factory, lot_underlying="FINNIFTY")
    broker = FakeBroker(candles=[], ticks=[])

    await TradeReplayer(build_executor(session_factory, broker), broker).replay_trade(trade_id)

    async with session_factory() as session:
        event = (await session.execute(select(TradeEvent))).scalar_one()
    assert event.payload_json == {"reason": "UNKNOWN_LOT_SIZE"}
    assert broker.resolve_calls == 0


async def test_live_watcher_trades_from_streamed_ticks(session_factory):
    trade_id = await create_complete_trade(session_factory)
    # LTP ticks arrive five minutes after the signal: out of range, in range, then stop loss.
    broker = FakeBroker(candles=[], ticks=[155, 144, 160, 120])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)

    assert await watcher.start_new_watches() == 1
    await wait_until_paper_trade_finishes(session_factory)
    await watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CLOSED.value
    assert paper.entry_price == 144
    assert paper.exit_reason == "SL_HIT"
    assert paper.exit_price == 120
    assert await watcher.start_new_watches() == 0  # nothing left to start


async def test_live_watcher_resumes_an_open_position_after_restart(session_factory):
    trade_id = await create_complete_trade(session_factory)
    first_broker = FakeBroker(candles=[], ticks=[155, 144, 160])
    first_watcher = LiveTradeWatcher(build_executor(session_factory, first_broker), first_broker, utc_clock)
    await first_watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await first_watcher.stop_all()

    second_broker = FakeBroker(candles=[], ticks=[130, 119])
    second_watcher = LiveTradeWatcher(build_executor(session_factory, second_broker), second_broker, utc_clock)
    assert await second_watcher.start_new_watches() == 1
    await wait_until_paper_trade_finishes(session_factory)
    await second_watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.entry_price == 144
    assert paper.exit_reason == "SL_HIT"
    assert second_broker.resolve_calls == 0  # instrument key was already stored


async def test_manual_stop_closes_an_open_position_at_the_latest_price(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[143, 150])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await asyncio.sleep(0.1)  # let the second tick through

    ledger = PaperTradeLedger(session_factory)
    assert await ledger.request_manual_stop(trade_id, utc_clock()) is StopRequestResult.REQUESTED
    assert await watcher.apply_stop_requests() == 1
    await watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CLOSED.value
    assert paper.exit_reason == "MANUAL"
    assert paper.exit_price == 150
    assert paper.pnl == (150 - 143) * 2 * LOT_SIZE
    assert (await load_one(session_factory, Trade)).state == TradeState.CLOSED.value
    assert await load_event_types(session_factory) == [
        EventType.ENTRY_TRIGGERED.value,
        EventType.MANUAL_STOP_REQUESTED.value,
        EventType.MANUAL_EXIT.value,
    ]


async def test_manual_stop_cancels_a_trade_that_has_not_entered_yet(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[155])  # never reaches the entry range
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()

    await PaperTradeLedger(session_factory).request_manual_stop(trade_id, utc_clock())
    await watcher.apply_stop_requests()
    await watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    assert (await load_one(session_factory, Trade)).state == TradeState.CANCELLED.value
    assert await load_event_types(session_factory) == [
        EventType.MANUAL_STOP_REQUESTED.value,
        EventType.CANCELLED.value,
    ]
    async with session_factory() as session:
        events = (await session.execute(select(TradeEvent).order_by(TradeEvent.id))).scalars().all()
    assert events[-1].payload_json == {"reason": "MANUAL_STOP"}


async def test_manual_stop_is_rejected_when_there_is_nothing_to_stop(session_factory):
    trade_id = await create_complete_trade(session_factory)
    ledger = PaperTradeLedger(session_factory)

    assert await ledger.request_manual_stop(trade_id, utc_clock()) is StopRequestResult.NOT_STOPPABLE
    assert await ledger.request_manual_stop(999, utc_clock()) is StopRequestResult.UNKNOWN_TRADE


async def test_a_second_manual_stop_request_is_recognised(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[155])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()
    ledger = PaperTradeLedger(session_factory)

    assert await ledger.request_manual_stop(trade_id, utc_clock()) is StopRequestResult.REQUESTED
    assert await ledger.request_manual_stop(trade_id, utc_clock()) is StopRequestResult.ALREADY_REQUESTED
    await watcher.stop_all()


after_market_close_clock = lambda: BASE_TIME + timedelta(hours=5, minutes=30)  # 15:30 IST


async def test_open_position_is_closed_at_market_close_even_without_a_new_tick(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[144, 160])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await asyncio.sleep(0.1)  # let the second tick through

    watcher._clock = after_market_close_clock
    assert await watcher.close_trades_at_market_close() == 1
    await watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CLOSED.value
    assert paper.exit_reason == "EOD"
    assert paper.exit_price == 160
    assert paper.pnl == (160 - 144) * 2 * LOT_SIZE
    assert (await load_one(session_factory, Trade)).state == TradeState.CLOSED.value


async def test_waiting_entry_is_cancelled_at_market_close(session_factory):
    await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[155])  # never reaches the entry range
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()

    watcher._clock = after_market_close_clock
    assert await watcher.close_trades_at_market_close() == 1
    await watcher.stop_all()

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CANCELLED.value
    assert (await load_one(session_factory, Trade)).state == TradeState.CANCELLED.value


async def test_unsettleable_trade_after_close_retries_the_price_once_a_minute_and_does_not_stream(
    session_factory,
):
    await create_complete_trade(session_factory)
    first_broker = FakeBroker(candles=[], ticks=[144])
    first_watcher = LiveTradeWatcher(build_executor(session_factory, first_broker), first_broker, utc_clock)
    await first_watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await first_watcher.stop_all()

    # Restarted after market close, and the contract no longer returns a quote.
    broker = FakeBroker(candles=[], ticks=[])
    broker.ltp_missing = True
    clock = {"now": after_market_close_clock()}
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, lambda: clock["now"])
    await watcher.start_new_watches()
    await asyncio.sleep(0.05)

    assert await watcher.close_trades_at_market_close() == 0
    assert await watcher.close_trades_at_market_close() == 0
    assert broker.ltp_calls == 1
    clock["now"] += timedelta(seconds=61)
    assert await watcher.close_trades_at_market_close() == 0
    await watcher.stop_all()

    assert broker.ltp_calls == 2
    assert broker.stream_calls == 0


async def test_failed_manual_stop_and_market_close_share_one_retry_gate(session_factory):
    trade_id = await create_complete_trade(session_factory)
    first_broker = FakeBroker(candles=[], ticks=[144])
    first_watcher = LiveTradeWatcher(build_executor(session_factory, first_broker), first_broker, utc_clock)
    await first_watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await first_watcher.stop_all()
    await PaperTradeLedger(session_factory).request_manual_stop(trade_id, utc_clock())

    broker = FakeBroker(candles=[], ticks=[])
    broker.ltp_missing = True
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, after_market_close_clock)
    await watcher.start_new_watches()
    await asyncio.sleep(0.05)

    await watcher.sync_watches()
    await watcher.sync_watches()
    await watcher.stop_all()

    assert broker.ltp_calls == 1


async def test_open_trade_without_a_price_can_be_closed_at_its_entry_price(session_factory):
    trade_id = await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[144])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)
    await watcher.stop_all()
    executor = build_executor(session_factory, FakeBroker(candles=[], ticks=[]))

    assert await executor.close_open_trade_at_entry_price(trade_id) is True
    assert await executor.close_open_trade_at_entry_price(trade_id) is False

    paper = await load_one(session_factory, PaperTrade)
    assert paper.status == PaperTradeStatus.CLOSED.value
    assert paper.exit_reason == "MANUAL"
    assert paper.exit_price == paper.entry_price == 144
    assert paper.pnl == 0
    assert (await load_one(session_factory, Trade)).state == TradeState.CLOSED.value
    assert (await load_event_types(session_factory))[-1] == EventType.MANUAL_EXIT.value


async def test_nothing_is_closed_before_market_close(session_factory):
    await create_complete_trade(session_factory)
    broker = FakeBroker(candles=[], ticks=[144])
    watcher = LiveTradeWatcher(build_executor(session_factory, broker), broker, utc_clock)
    await watcher.start_new_watches()
    await wait_until(session_factory, lambda p: p.status == PaperTradeStatus.OPEN.value)

    assert await watcher.close_trades_at_market_close() == 0
    await watcher.stop_all()


async def wait_until_paper_trade_finishes(session_factory) -> None:
    await wait_until(
        session_factory,
        lambda p: p.status in (PaperTradeStatus.CLOSED.value, PaperTradeStatus.CANCELLED.value),
    )


async def wait_until(session_factory, condition) -> None:
    for _ in range(200):
        async with session_factory() as session:
            paper = (await session.execute(select(PaperTrade))).scalar_one_or_none()
        if paper is not None and condition(paper):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")
