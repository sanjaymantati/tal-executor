"""Replay a stored trade over historical 1-minute candles.

Assumption (conservative): inside one candle the order of price moves is unknown, so whenever a
single candle touches both the stop loss and the target, the stop loss is taken first. See
`RiskRewardExit` and `PaperPositionMachine` for the exact rules.
"""

from datetime import timedelta

import structlog

from app.broker.base import BrokerClient, Candle
from app.execution.executor import PaperTradeExecutor, PreparedTrade
from app.execution.types import PriceBar

log = structlog.get_logger(__name__)

CANDLE_INTERVAL = "1minute"
CANDLE_LENGTH = timedelta(minutes=1)


class TradeReplayer:
    def __init__(self, executor: PaperTradeExecutor, broker: BrokerClient) -> None:
        self._executor = executor
        self._broker = broker

    async def replay_all_new_trades(self) -> int:
        replayed = 0
        for trade_id in await self._executor.list_new_trade_ids():
            if await self.replay_trade(trade_id):
                replayed += 1
        return replayed

    async def replay_trade(self, trade_id: int) -> bool:
        """Returns True when a paper trade was created and simulated."""
        if trade_id not in await self._executor.list_new_trade_ids():
            log.info("replay_skipped", trade_id=trade_id)
            return False
        prepared = await self._executor.prepare_new_trade(trade_id)
        if prepared is None:
            return True
        candles = await self._load_candles(prepared)
        await self._run_candles(prepared, candles)
        return True

    async def _load_candles(self, prepared: PreparedTrade) -> list[Candle]:
        available_at = prepared.context.available_at
        candles = await self._broker.get_historical_candles(
            prepared.instrument_key,
            CANDLE_INTERVAL,
            available_at,
            prepared.plan.market_close_at,
        )
        # A candle starting before the signal contains prices from before it existed.
        return [candle for candle in candles if candle.time > available_at]

    async def _run_candles(self, prepared: PreparedTrade, candles: list[Candle]) -> None:
        for candle in candles:
            bar = PriceBar(candle.time, candle.open, candle.high, candle.low, candle.close)
            if await self._executor.feed_bar(prepared, bar):
                return
        await self._finish_without_more_data(prepared, candles)

    async def _finish_without_more_data(
        self, prepared: PreparedTrade, candles: list[Candle]
    ) -> None:
        if candles:
            last = candles[-1]
            await self._executor.finish_session(prepared, last.close, last.time + CANDLE_LENGTH)
        else:
            await self._executor.finish_session(
                prepared, prepared.plan.entry_low, prepared.plan.market_close_at
            )
