from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.broker.base import BrokerClient, InstrumentNotFoundError
from app.config import ExecutionConfig, UnknownLotSizeError
from app.enums import PaperTradeStatus
from app.execution.exit_strategies import ExitStrategy, build_exit_strategy
from app.execution.ledger import PaperTradeLedger, TradeContext
from app.execution.machine import PaperPositionMachine
from app.execution.types import PriceBar, TradePlan, TradingCosts
from app.timeutil import IST


@dataclass
class PreparedTrade:
    context: TradeContext
    instrument_key: str
    plan: TradePlan
    machine: PaperPositionMachine


class PaperTradeExecutor:
    """Prepares paper trades and feeds them price bars. Source of bars is the caller's concern."""

    def __init__(
        self,
        ledger: PaperTradeLedger,
        broker: BrokerClient,
        config: ExecutionConfig,
        clock: Callable[[], datetime],
    ) -> None:
        self._ledger = ledger
        self._broker = broker
        self._config = config
        self._clock = clock
        self._exit_strategy: ExitStrategy = build_exit_strategy(config)
        self._costs = TradingCosts(config.slippage, config.brokerage_per_order)

    async def list_new_trade_ids(self) -> list[int]:
        return await self._ledger.list_complete_trade_ids_without_paper_trade()

    async def list_unfinished_trade_ids(self) -> list[int]:
        return await self._ledger.list_unfinished_trade_ids()

    async def prepare_new_trade(self, trade_id: int) -> PreparedTrade | None:
        """Creates the paper trade. Returns None when it was cancelled before it could start."""
        context = await self._ledger.load_trade_context(trade_id)
        try:
            quantity = self._config.quantity_for(context.underlying)
            instrument_key = await self._resolve_instrument_key(context)
        except (UnknownLotSizeError, InstrumentNotFoundError) as error:
            await self._ledger.cancel_unstarted_trade(
                trade_id, error.cancel_reason, self._clock(), self._config
            )
            return None
        await self._ledger.open_paper_trade(trade_id, instrument_key, quantity, self._config)
        return self._assemble(context, instrument_key, quantity)

    async def prepare_unfinished_trade(self, trade_id: int) -> PreparedTrade:
        """Rebuilds in-memory state for a paper trade left running by an earlier process."""
        context = await self._ledger.load_trade_context(trade_id)
        saved = await self._ledger.load_saved_position(trade_id)
        prepared = self._assemble(context, context.instrument_key, saved.quantity)
        if saved.status is PaperTradeStatus.OPEN:
            prepared.machine.restore_open_position(
                saved.entry_price, saved.entry_time, set(saved.touched_levels)
            )
        return prepared

    async def feed_bar(self, prepared: PreparedTrade, bar: PriceBar) -> bool:
        """Returns True once the paper trade has finished."""
        events = prepared.machine.on_bar(bar)
        await self._persist(prepared, events)
        return prepared.machine.is_finished

    async def finish_session(self, prepared: PreparedTrade, price: float, time: datetime) -> None:
        events = prepared.machine.end_session(price, time)
        await self._persist(prepared, events)

    async def has_pending_stop_request(self, trade_id: int) -> bool:
        return await self._ledger.has_pending_stop_request(trade_id)

    async def stop_manually(self, prepared: PreparedTrade, price: float | None, time: datetime) -> None:
        events = prepared.machine.stop_manually(price, time)
        await self._persist(prepared, events)

    async def _persist(self, prepared: PreparedTrade, events: list) -> None:
        if events:
            await self._ledger.apply_events(prepared.context.trade_id, prepared.machine, events)

    async def _resolve_instrument_key(self, context: TradeContext) -> str:
        if context.instrument_key:
            return context.instrument_key
        instrument = await self._broker.resolve_instrument(
            context.underlying, context.expiry, context.strike, context.option_type
        )
        return instrument.instrument_key

    def _assemble(self, context: TradeContext, instrument_key: str, quantity: int) -> PreparedTrade:
        plan = self._build_plan(context, quantity)
        machine = PaperPositionMachine(plan, self._exit_strategy, self._costs)
        return PreparedTrade(context, instrument_key, plan, machine)

    def _build_plan(self, context: TradeContext, quantity: int) -> TradePlan:
        return TradePlan(
            entry_low=context.entry_low,
            entry_high=context.entry_high,
            stop_loss=context.stop_loss,
            targets=context.targets,
            quantity=quantity,
            entry_timeout_at=context.available_at + timedelta(minutes=self._config.entry_timeout_min),
            market_close_at=self._find_market_close(context.available_at),
        )

    def _find_market_close(self, available_at: datetime) -> datetime:
        signal_day = available_at.astimezone(IST).date()
        close_ist = datetime.combine(signal_day, self._config.market_close_exit, tzinfo=IST)
        return close_ist.astimezone(timezone.utc)
