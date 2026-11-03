from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from app.audit.events import record_trade_event
from app.config import ExecutionConfig
from app.db.models import (
    ConfigSnapshot,
    PaperTrade,
    RawMessage,
    Trade,
    TradeEvent,
    TradeMessage,
)
from app.db.session import SessionFactory
from app.enums import (
    CancelReason,
    EventType,
    PaperTradeStatus,
    StopRequestResult,
    TradeMessageRole,
    TradeState,
)
from app.execution.machine import PaperPositionMachine
from app.execution.types import MachineEvent

UNFINISHED_STATUSES = (PaperTradeStatus.PENDING.value, PaperTradeStatus.OPEN.value)
STATE_BY_PAPER_STATUS = {
    PaperTradeStatus.OPEN: TradeState.ACTIVE,
    PaperTradeStatus.CLOSED: TradeState.CLOSED,
    PaperTradeStatus.CANCELLED: TradeState.CANCELLED,
}


@dataclass(frozen=True)
class TradeContext:
    trade_id: int
    underlying: str
    expiry: str | None
    strike: float
    option_type: str
    instrument_key: str | None
    entry_low: float
    entry_high: float
    stop_loss: float
    targets: tuple[float, ...]
    available_at: datetime  # when the trade became actionable: its latest linked message


@dataclass(frozen=True)
class SavedPosition:
    quantity: int
    status: PaperTradeStatus
    entry_price: float | None
    entry_time: datetime | None
    touched_levels: frozenset[int]


class PaperTradeLedger:
    """All database reads and writes for paper trading."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def list_complete_trade_ids_without_paper_trade(self) -> list[int]:
        has_paper_trade = select(PaperTrade.id).where(PaperTrade.trade_id == Trade.id)
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trade.id)
                .where(Trade.state == TradeState.COMPLETE.value, ~has_paper_trade.exists())
                .order_by(Trade.id)
            )
            return list(result.scalars())

    async def list_unfinished_trade_ids(self) -> list[int]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PaperTrade.trade_id)
                .where(PaperTrade.status.in_(UNFINISHED_STATUSES))
                .order_by(PaperTrade.trade_id)
            )
            return list(result.scalars())

    async def load_trade_context(self, trade_id: int) -> TradeContext:
        async with self._session_factory() as session:
            trade = await session.get(Trade, trade_id)
            available_at = await self._find_available_at(session, trade_id)
        return TradeContext(
            trade_id=trade.id,
            underlying=trade.underlying,
            expiry=trade.expiry,
            strike=trade.strike,
            option_type=trade.option_type,
            instrument_key=trade.instrument_key,
            entry_low=trade.entry_low,
            entry_high=trade.entry_high,
            stop_loss=trade.stop_loss,
            targets=tuple(trade.targets_json),
            available_at=available_at,
        )

    async def load_saved_position(self, trade_id: int) -> SavedPosition:
        async with self._session_factory() as session:
            paper = await self._find_paper_trade(session, trade_id)
            events = await session.execute(
                select(TradeEvent.payload_json).where(
                    TradeEvent.trade_id == trade_id,
                    TradeEvent.event_type == EventType.TARGET_LEVEL_TOUCHED.value,
                )
            )
            touched = frozenset(int(payload["level"][1:]) for payload in events.scalars())
        return SavedPosition(
            quantity=paper.qty,
            status=PaperTradeStatus(paper.status),
            entry_price=paper.entry_price,
            entry_time=paper.entry_time,
            touched_levels=touched,
        )

    async def has_paper_trade(self, trade_id: int) -> bool:
        async with self._session_factory() as session:
            return await self._find_paper_trade(session, trade_id) is not None

    async def open_paper_trade(
        self, trade_id: int, instrument_key: str, quantity: int, config: ExecutionConfig
    ) -> None:
        async with self._session_factory.begin() as session:
            trade = await session.get(Trade, trade_id)
            trade.instrument_key = instrument_key
            session.add(
                PaperTrade(
                    trade_id=trade_id,
                    qty=quantity,
                    status=PaperTradeStatus.PENDING.value,
                )
            )
            session.add(
                ConfigSnapshot(trade_id=trade_id, config_json=config.model_dump(mode="json"))
            )

    async def cancel_unstarted_trade(
        self,
        trade_id: int,
        reason: CancelReason,
        time: datetime,
        config: ExecutionConfig,
        message: str | None = None,
    ) -> None:
        async with self._session_factory.begin() as session:
            trade = await session.get(Trade, trade_id)
            trade.state = TradeState.CANCELLED.value
            session.add(PaperTrade(trade_id=trade_id, status=PaperTradeStatus.CANCELLED.value))
            session.add(
                ConfigSnapshot(trade_id=trade_id, config_json=config.model_dump(mode="json"))
            )
            record_trade_event(
                session,
                trade_id,
                EventType.CANCELLED,
                time,
                None,
                None,
                build_cancel_payload(reason, message),
            )

    async def request_manual_stop(self, trade_id: int, time: datetime) -> StopRequestResult:
        """Queues an operator stop. The process running the trade picks it up and closes the position."""
        async with self._session_factory.begin() as session:
            if await session.get(Trade, trade_id) is None:
                return StopRequestResult.UNKNOWN_TRADE
            paper = await self._find_paper_trade(session, trade_id)
            if paper is None or paper.status not in UNFINISHED_STATUSES:
                return StopRequestResult.NOT_STOPPABLE
            if await self._has_stop_request(session, trade_id):
                return StopRequestResult.ALREADY_REQUESTED
            record_trade_event(
                session, trade_id, EventType.MANUAL_STOP_REQUESTED, time, None, None, None
            )
            return StopRequestResult.REQUESTED

    async def has_pending_stop_request(self, trade_id: int) -> bool:
        async with self._session_factory() as session:
            return await self._has_stop_request(session, trade_id)

    async def apply_events(
        self, trade_id: int, machine: PaperPositionMachine, events: list[MachineEvent]
    ) -> None:
        async with self._session_factory.begin() as session:
            for event in events:
                record_trade_event(
                    session, trade_id, event.event_type, event.time, event.price, None, event.payload
                )
            paper = await self._find_paper_trade(session, trade_id)
            self._sync_paper_trade(paper, machine)
            trade = await session.get(Trade, trade_id)
            new_state = STATE_BY_PAPER_STATUS.get(machine.status)
            if new_state is not None:
                trade.state = new_state.value

    def _sync_paper_trade(self, paper: PaperTrade, machine: PaperPositionMachine) -> None:
        paper.status = machine.status.value
        if machine.position is not None:
            paper.entry_price = machine.position.entry_price
            paper.entry_time = machine.position.entry_time
        if machine.result is not None:
            paper.exit_price = machine.result.exit_price
            paper.exit_time = machine.result.exit_time
            paper.exit_reason = machine.result.exit_reason.value
            paper.pnl = machine.result.pnl
            paper.r_multiple = machine.result.r_multiple
            paper.fees = machine.result.fees

    async def _has_stop_request(self, session, trade_id: int) -> bool:
        result = await session.execute(
            select(TradeEvent.id)
            .where(
                TradeEvent.trade_id == trade_id,
                TradeEvent.event_type == EventType.MANUAL_STOP_REQUESTED.value,
            )
            .limit(1)
        )
        return result.first() is not None

    async def _find_paper_trade(self, session, trade_id: int) -> PaperTrade | None:
        result = await session.execute(select(PaperTrade).where(PaperTrade.trade_id == trade_id))
        return result.scalar_one_or_none()

    async def _find_available_at(self, session, trade_id: int) -> datetime:
        result = await session.execute(
            select(func.max(RawMessage.sent_at))
            .join(TradeMessage, TradeMessage.message_id == RawMessage.id)
            .where(
                TradeMessage.trade_id == trade_id,
                TradeMessage.role != TradeMessageRole.PREMIUM.value,
            )
        )
        return result.scalar_one()


def build_cancel_payload(reason: CancelReason, message: str | None) -> dict[str, str]:
    payload = {"reason": reason.value}
    if message is not None:
        payload["message"] = message
    return payload
