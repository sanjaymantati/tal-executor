import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from app.broker.base import BrokerClient
from app.enums import PaperTradeStatus
from app.execution.executor import PaperTradeExecutor, PreparedTrade
from app.execution.types import PriceBar

log = structlog.get_logger(__name__)


@dataclass
class Watch:
    """One paper trade being followed live. The lock keeps ticks and manual stops from interleaving."""

    prepared: PreparedTrade
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_price: float | None = None
    last_time: datetime | None = None
    task: asyncio.Task | None = None

    @property
    def trade_id(self) -> int:
        return self.prepared.context.trade_id

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()


class LiveTradeWatcher:
    """Paper-trades COMPLETE trades in real time from the broker's LTP stream."""

    def __init__(
        self,
        executor: PaperTradeExecutor,
        broker: BrokerClient,
        clock: Callable[[], datetime],
    ) -> None:
        self._executor = executor
        self._broker = broker
        self._clock = clock
        self._watches: dict[int, Watch] = {}

    async def sync_watches(self) -> None:
        """One scheduler cycle: follow new trades, then honour operator stop requests."""
        await self.start_new_watches()
        await self.apply_stop_requests()

    async def start_new_watches(self) -> int:
        started = await self._resume_unfinished_trades()
        started += await self._start_new_trades()
        return started

    async def apply_stop_requests(self) -> int:
        stopped = 0
        for watch in list(self._watches.values()):
            if watch.is_running and await self._executor.has_pending_stop_request(watch.trade_id):
                stopped += await self._stop_manually(watch)
        return stopped

    async def stop_all(self) -> None:
        tasks = [watch.task for watch in self._watches.values() if watch.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._watches.clear()

    async def _resume_unfinished_trades(self) -> int:
        started = 0
        for trade_id in await self._executor.list_unfinished_trade_ids():
            if self._is_watching(trade_id):
                continue
            prepared = await self._executor.prepare_unfinished_trade(trade_id)
            self._start_watching(prepared)
            started += 1
        return started

    async def _start_new_trades(self) -> int:
        started = 0
        for trade_id in await self._executor.list_new_trade_ids():
            prepared = await self._executor.prepare_new_trade(trade_id)
            if prepared is not None:
                self._start_watching(prepared)
                started += 1
        return started

    def _is_watching(self, trade_id: int) -> bool:
        watch = self._watches.get(trade_id)
        return watch is not None and watch.is_running

    def _start_watching(self, prepared: PreparedTrade) -> None:
        watch = Watch(prepared)
        watch.task = asyncio.create_task(self._watch(watch))
        self._watches[watch.trade_id] = watch
        log.info("watching_trade", trade_id=watch.trade_id, instrument_key=prepared.instrument_key)

    async def _watch(self, watch: Watch) -> None:
        async def on_ltp(instrument_key: str, price: float, time: datetime) -> None:
            watch.last_price, watch.last_time = price, time
            async with watch.lock:
                finished = await self._executor.feed_bar(watch.prepared, PriceBar.from_tick(time, price))
            if finished:
                watch.finished.set()

        stream_task = asyncio.create_task(
            self._broker.stream_ltp([watch.prepared.instrument_key], on_ltp)
        )
        finished_task = asyncio.create_task(watch.finished.wait())
        try:
            await asyncio.wait({stream_task, finished_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stream_task.cancel()
            finished_task.cancel()
        self._log_stream_failure(watch.prepared, stream_task)

    async def _stop_manually(self, watch: Watch) -> int:
        """Returns 1 when the trade was stopped; 0 when no price was available yet (retried next cycle)."""
        try:
            price = await self._find_stop_price(watch)
        except Exception as error:
            log.warning("manual_stop_price_unavailable", trade_id=watch.trade_id, error=repr(error))
            return 0
        async with watch.lock:
            await self._executor.stop_manually(watch.prepared, price, self._clock())
        watch.finished.set()
        log.info("manual_stop_applied", trade_id=watch.trade_id, price=price)
        return 1

    async def _find_stop_price(self, watch: Watch) -> float | None:
        """A position that never entered needs no price; an open one exits at the latest known price."""
        if watch.prepared.machine.status is not PaperTradeStatus.OPEN:
            return None
        if watch.last_price is not None:
            return watch.last_price
        return await self._broker.get_ltp(watch.prepared.instrument_key)

    def _log_stream_failure(self, prepared: PreparedTrade, stream_task: asyncio.Task) -> None:
        if stream_task.cancelled() or not stream_task.done():
            return
        error = stream_task.exception()
        if error is not None:
            log.error(
                "ltp_stream_failed", trade_id=prepared.context.trade_id, error=repr(error)
            )
