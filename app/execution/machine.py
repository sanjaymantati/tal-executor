from datetime import datetime

from app.enums import CancelReason, EventType, ExitReason, PaperTradeStatus
from app.execution.exit_strategies import ExitStrategy
from app.execution.types import (
    ExitDecision,
    MachineEvent,
    OpenPosition,
    PriceBar,
    TradePlan,
    TradeResult,
    TradingCosts,
)

EXIT_EVENT_BY_REASON = {
    ExitReason.SL_HIT: EventType.SL_HIT,
    ExitReason.RR_TARGET: EventType.RR_TARGET_HIT,
    ExitReason.EOD: EventType.EOD_EXIT,
    ExitReason.MANUAL: EventType.MANUAL_EXIT,
}
ORDERS_PER_ROUND_TRIP = 2


class PaperPositionMachine:
    """Pure simulation of one paper trade. No I/O; fed price bars, returns audit events.

    Live ticks and historical candles use the same path: a tick is a degenerate bar.
    """

    def __init__(
        self, plan: TradePlan, exit_strategy: ExitStrategy, costs: TradingCosts
    ) -> None:
        self._plan = plan
        self._exit_strategy = exit_strategy
        self._costs = costs
        self.status = PaperTradeStatus.PENDING
        self.position: OpenPosition | None = None
        self.result: TradeResult | None = None
        self.cancel_reason: CancelReason | None = None

    @property
    def is_finished(self) -> bool:
        return self.status in (PaperTradeStatus.CLOSED, PaperTradeStatus.CANCELLED)

    def restore_open_position(
        self, entry_price: float, entry_time: datetime, touched_levels: set[int]
    ) -> None:
        self.status = PaperTradeStatus.OPEN
        self.position = OpenPosition(
            entry_price=entry_price,
            entry_time=entry_time,
            stop_loss=self._plan.stop_loss,
            targets=self._plan.targets,
            touched_levels=set(touched_levels),
        )

    def on_bar(self, bar: PriceBar) -> list[MachineEvent]:
        if self.is_finished:
            return []
        if self.status is PaperTradeStatus.PENDING:
            return self._handle_pending_bar(bar)
        return self._evaluate_exit(bar, entered_on_this_bar=False)

    def end_session(self, price: float, time: datetime) -> list[MachineEvent]:
        """Called when no more prices will arrive (e.g. candles ran out at market close)."""
        if self.status is PaperTradeStatus.OPEN:
            return self._close(ExitDecision(ExitReason.EOD, price), time)
        if self.status is PaperTradeStatus.PENDING:
            return self.cancel(CancelReason.MARKET_CLOSE, time)
        return []

    def stop_manually(self, price: float | None, time: datetime) -> list[MachineEvent]:
        """Operator stop: closes an open position at `price`, or cancels a trade still waiting to enter."""
        if self.status is PaperTradeStatus.OPEN:
            return self._close(ExitDecision(ExitReason.MANUAL, price), time)
        return self.cancel(CancelReason.MANUAL_STOP, time)

    def cancel(self, reason: CancelReason, time: datetime) -> list[MachineEvent]:
        if self.is_finished:
            return []
        self.status = PaperTradeStatus.CANCELLED
        self.cancel_reason = reason
        return [MachineEvent(EventType.CANCELLED, None, time, {"reason": reason.value})]

    def _handle_pending_bar(self, bar: PriceBar) -> list[MachineEvent]:
        cancel_reason = self._find_entry_cancel_reason(bar)
        if cancel_reason is not None:
            return self.cancel(cancel_reason, bar.time)
        if not self._overlaps_entry_range(bar):
            return []
        entry_events = self._enter(bar)
        if self.status is not PaperTradeStatus.OPEN:
            return entry_events
        return entry_events + self._evaluate_exit(bar, entered_on_this_bar=True)

    def _find_entry_cancel_reason(self, bar: PriceBar) -> CancelReason | None:
        if self._plan.entry_low <= self._plan.stop_loss:
            return CancelReason.INVALID_LEVELS
        if bar.time >= self._plan.market_close_at:
            return CancelReason.MARKET_CLOSE
        if bar.time >= self._plan.entry_timeout_at:
            return CancelReason.ENTRY_TIMEOUT
        if not self._overlaps_entry_range(bar) and bar.low <= self._plan.stop_loss:
            return CancelReason.SL_BEFORE_ENTRY
        return None

    def _overlaps_entry_range(self, bar: PriceBar) -> bool:
        return bar.low <= self._plan.entry_high and bar.high >= self._plan.entry_low

    def _enter(self, bar: PriceBar) -> list[MachineEvent]:
        fill_price = min(max(bar.open, self._plan.entry_low), self._plan.entry_high)
        entry_price = fill_price + self._costs.slippage
        self.status = PaperTradeStatus.OPEN
        self.position = OpenPosition(
            entry_price=entry_price,
            entry_time=bar.time,
            stop_loss=self._plan.stop_loss,
            targets=self._plan.targets,
        )
        payload = {"fill_price": fill_price, "slippage": self._costs.slippage}
        return [MachineEvent(EventType.ENTRY_TRIGGERED, entry_price, bar.time, payload)]

    def _evaluate_exit(self, bar: PriceBar, entered_on_this_bar: bool) -> list[MachineEvent]:
        if bar.time >= self._plan.market_close_at:
            return self._close(ExitDecision(ExitReason.EOD, bar.open), bar.time)
        decision = self._exit_strategy.decide_exit(self.position, bar, entered_on_this_bar)
        stop_loss_hit = decision is not None and decision.reason is ExitReason.SL_HIT
        events: list[MachineEvent] = []
        if not entered_on_this_bar and not stop_loss_hit:
            events += self._record_touched_levels(bar)
        if decision is not None:
            events += self._close(decision, bar.time)
        return events

    def _record_touched_levels(self, bar: PriceBar) -> list[MachineEvent]:
        events = []
        for level, target_price in enumerate(self.position.targets, start=1):
            if level in self.position.touched_levels or bar.high < target_price:
                continue
            self.position.touched_levels.add(level)
            payload = {"level": f"T{level}", "target_price": target_price}
            events.append(
                MachineEvent(EventType.TARGET_LEVEL_TOUCHED, target_price, bar.time, payload)
            )
        return events

    def _close(self, decision: ExitDecision, time: datetime) -> list[MachineEvent]:
        position = self.position
        exit_price = decision.price - self._costs.slippage
        fees = self._costs.brokerage_per_order * ORDERS_PER_ROUND_TRIP
        price_move = exit_price - position.entry_price
        pnl = price_move * self._plan.quantity - fees
        self.result = TradeResult(
            exit_price=exit_price,
            exit_time=time,
            exit_reason=decision.reason,
            pnl=pnl,
            r_multiple=price_move / position.risk,
            fees=fees,
        )
        self.status = PaperTradeStatus.CLOSED
        payload = {
            "reason": decision.reason.value,
            "pnl": pnl,
            "r_multiple": self.result.r_multiple,
            "fees": fees,
        }
        return [MachineEvent(EXIT_EVENT_BY_REASON[decision.reason], exit_price, time, payload)]
