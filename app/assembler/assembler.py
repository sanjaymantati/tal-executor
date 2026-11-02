import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assembler.matching import OPEN_FOR_MERGE_STATES, TradeMatcher
from app.audit.events import record_trade_event
from app.db.models import (
    MessageClassification,
    RawMessage,
    Trade,
    TradeFieldHistory,
    TradeMessage,
)
from app.db.session import SessionFactory
from app.enums import (
    ClassificationStatus,
    EventType,
    MessageType,
    TradeMessageRole,
    TradeState,
)
from app.llm.schemas import SignalFields

log = structlog.get_logger(__name__)

SCALAR_FIELD_NAMES = (
    "underlying",
    "expiry",
    "strike",
    "option_type",
    "entry_low",
    "entry_high",
    "stop_loss",
)
UPDATE_MESSAGE_TYPES = (MessageType.SIGNAL_UPDATE, MessageType.ENTRY_TRIGGER)
ROLE_BY_MESSAGE_TYPE = {
    MessageType.NEW_SIGNAL: TradeMessageRole.ORIGIN,
    MessageType.SIGNAL_UPDATE: TradeMessageRole.UPDATE,
    MessageType.ENTRY_TRIGGER: TradeMessageRole.TRIGGER,
    MessageType.PREMIUM_UPDATE: TradeMessageRole.PREMIUM,
}


class TradeAssembler:
    """Turns classified messages into trades; one trade may span several messages."""

    def __init__(
        self,
        session_factory: SessionFactory,
        matcher: TradeMatcher,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._matcher = matcher
        self._clock = clock

    async def assemble_pending(self) -> int:
        classification_ids = await self._list_pending_classification_ids()
        for classification_id in classification_ids:
            await self._assemble_classification(classification_id)
        return len(classification_ids)

    async def expire_incomplete_trades(self) -> int:
        now = self._clock()
        async with self._session_factory.begin() as session:
            idle_trades = await self._matcher.find_partial_trades_idle_since(session, now)
            for trade in idle_trades:
                trade.state = TradeState.EXPIRED_INCOMPLETE.value
                record_trade_event(
                    session, trade.id, EventType.EXPIRED, now, None, None, self._snapshot(trade)
                )
        return len(idle_trades)

    async def _list_pending_classification_ids(self) -> list[int]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessageClassification.id)
                .join(RawMessage, RawMessage.id == MessageClassification.message_id)
                .where(
                    MessageClassification.status == ClassificationStatus.OK.value,
                    MessageClassification.assembled_at.is_(None),
                )
                .order_by(RawMessage.tg_message_id, MessageClassification.id)
            )
            return list(result.scalars())

    async def _assemble_classification(self, classification_id: int) -> None:
        async with self._session_factory.begin() as session:
            classification = await session.get(MessageClassification, classification_id)
            message = await session.get(RawMessage, classification.message_id)
            await self._dispatch(session, classification, message)
            classification.assembled_at = self._clock()

    async def _dispatch(
        self, session: AsyncSession, classification: MessageClassification, message: RawMessage
    ) -> None:
        message_type = MessageType(classification.message_type)
        fields = SignalFields.model_validate(classification.fields_json)
        if message_type is MessageType.NEW_SIGNAL:
            await self._assemble_new_signal(session, message, fields)
        elif message_type in UPDATE_MESSAGE_TYPES:
            await self._assemble_update(
                session, message, message_type, fields, classification.references_msg_id
            )
        elif message_type is MessageType.PREMIUM_UPDATE:
            await self._assemble_premium_update(session, message, classification.references_msg_id)

    async def _assemble_new_signal(
        self, session: AsyncSession, message: RawMessage, fields: SignalFields
    ) -> None:
        edited_trade = await self._matcher.find_trade_linked_to_message(session, message.id)
        if edited_trade is not None:
            await self._merge_and_promote(session, edited_trade, message, fields, overwrite=True)
            return
        duplicate = await self._matcher.find_duplicate_trade(session, message, fields)
        if duplicate is not None:
            await self._link_message(session, duplicate, message, TradeMessageRole.UPDATE)
            await self._merge_and_promote(session, duplicate, message, fields, overwrite=False)
            return
        trade = await self._create_trade(session, message)
        await self._merge_and_promote(session, trade, message, fields, overwrite=False)

    async def _assemble_update(
        self,
        session: AsyncSession,
        message: RawMessage,
        message_type: MessageType,
        fields: SignalFields,
        references_msg_id: int | None,
    ) -> None:
        trade = await self._matcher.find_trade_for_update(session, message, references_msg_id)
        if trade is None:
            log.info("update_without_trade", tg_message_id=message.tg_message_id)
            return
        await self._link_message(session, trade, message, ROLE_BY_MESSAGE_TYPE[message_type])
        if trade.state not in OPEN_FOR_MERGE_STATES:
            return
        overwrite = message_type is MessageType.SIGNAL_UPDATE
        await self._merge_and_promote(session, trade, message, fields, overwrite=overwrite)

    async def _assemble_premium_update(
        self, session: AsyncSession, message: RawMessage, references_msg_id: int | None
    ) -> None:
        trade = await self._matcher.find_trade_linked_to_message(session, message.id)
        trade = trade or await self._matcher.find_trade_by_reply(session, message)
        trade = trade or await self._matcher.find_trade_by_reference(
            session, message, references_msg_id
        )
        if trade is not None:
            await self._link_message(session, trade, message, TradeMessageRole.PREMIUM)

    async def _create_trade(self, session: AsyncSession, message: RawMessage) -> Trade:
        trade = Trade(
            state=TradeState.PARTIAL.value, first_message_id=message.id, targets_json=[]
        )
        session.add(trade)
        await session.flush()
        await self._link_message(session, trade, message, TradeMessageRole.ORIGIN)
        record_trade_event(
            session,
            trade.id,
            EventType.CAPTURED,
            self._clock(),
            None,
            message.id,
            {"sent_at": message.sent_at.isoformat(), "text": message.text},
        )
        return trade

    async def _link_message(
        self, session: AsyncSession, trade: Trade, message: RawMessage, role: TradeMessageRole
    ) -> None:
        if await session.get(TradeMessage, (trade.id, message.id)) is None:
            session.add(TradeMessage(trade_id=trade.id, message_id=message.id, role=role.value))
            await session.flush()

    async def _merge_and_promote(
        self,
        session: AsyncSession,
        trade: Trade,
        message: RawMessage,
        fields: SignalFields,
        overwrite: bool,
    ) -> None:
        changes = self._merge_fields(session, trade, message, fields, overwrite)
        if changes:
            record_trade_event(
                session, trade.id, EventType.FIELD_UPDATED, self._clock(), None, message.id, changes
            )
        self._promote_if_complete(session, trade, message)

    def _merge_fields(
        self,
        session: AsyncSession,
        trade: Trade,
        message: RawMessage,
        fields: SignalFields,
        overwrite: bool,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for name, new_value in extract_present_fields(fields).items():
            old_value = read_trade_field(trade, name)
            if is_present(old_value) and not overwrite:
                continue
            if old_value == new_value:
                continue
            write_trade_field(trade, name, new_value)
            self._record_history(session, trade, message, name, old_value, new_value)
            changes[name] = {"old": old_value, "new": new_value}
        return changes

    def _record_history(
        self,
        session: AsyncSession,
        trade: Trade,
        message: RawMessage,
        name: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        session.add(
            TradeFieldHistory(
                trade_id=trade.id,
                field=name,
                old_value=json.dumps(old_value),
                new_value=json.dumps(new_value),
                message_id=message.id,
                changed_at=self._clock(),
            )
        )

    def _promote_if_complete(self, session: AsyncSession, trade: Trade, message: RawMessage) -> None:
        if trade.state != TradeState.PARTIAL.value or not is_trade_complete(trade):
            return
        now = self._clock()
        trade.state = TradeState.COMPLETE.value
        trade.completed_at = now
        record_trade_event(
            session, trade.id, EventType.COMPLETE, now, None, message.id, self._snapshot(trade)
        )

    def _snapshot(self, trade: Trade) -> dict[str, Any]:
        return {name: read_trade_field(trade, name) for name in (*SCALAR_FIELD_NAMES, "targets")}


def extract_present_fields(fields: SignalFields) -> dict[str, Any]:
    present: dict[str, Any] = {}
    for name in SCALAR_FIELD_NAMES:
        value = getattr(fields, name)
        if value is not None:
            present[name] = value.value if name == "option_type" else value
    if fields.targets:
        present["targets"] = list(fields.targets)
    return present


def read_trade_field(trade: Trade, name: str) -> Any:
    if name == "targets":
        return list(trade.targets_json or [])
    return getattr(trade, name)


def write_trade_field(trade: Trade, name: str, value: Any) -> None:
    if name == "targets":
        trade.targets_json = list(value)
    else:
        setattr(trade, name, value)


def is_present(value: Any) -> bool:
    return value is not None and value != []


def is_trade_complete(trade: Trade) -> bool:
    required = (
        trade.underlying,
        trade.strike,
        trade.option_type,
        trade.entry_low,
        trade.entry_high,
        trade.stop_loss,
        trade.targets_json,
    )
    return all(is_present(value) for value in required)
