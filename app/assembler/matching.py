from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RawMessage, Trade, TradeMessage
from app.enums import TradeState
from app.llm.schemas import SignalFields

OPEN_FOR_MERGE_STATES = (TradeState.PARTIAL.value, TradeState.COMPLETE.value)


class TradeMatcher:
    """Finds which trade a message belongs to. Pure lookup, never writes."""

    def __init__(self, window: timedelta) -> None:
        self._window = window

    async def find_trade_for_update(
        self, session: AsyncSession, message: RawMessage, references_msg_id: int | None
    ) -> Trade | None:
        """Priority: already linked, reply link, LLM reference, most recent PARTIAL in window."""
        trade = await self.find_trade_linked_to_message(session, message.id)
        if trade is None:
            trade = await self.find_trade_by_reply(session, message)
        if trade is None:
            trade = await self.find_trade_by_reference(session, message, references_msg_id)
        if trade is None:
            trade = await self.find_recent_partial_trade(session, message)
        return trade

    async def find_trade_linked_to_message(
        self, session: AsyncSession, message_id: int
    ) -> Trade | None:
        result = await session.execute(
            select(Trade)
            .join(TradeMessage, TradeMessage.trade_id == Trade.id)
            .where(TradeMessage.message_id == message_id)
            .order_by(Trade.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_trade_by_reply(self, session: AsyncSession, message: RawMessage) -> Trade | None:
        return await self._find_trade_by_tg_message_id(session, message, message.reply_to_msg_id)

    async def find_trade_by_reference(
        self, session: AsyncSession, message: RawMessage, references_msg_id: int | None
    ) -> Trade | None:
        return await self._find_trade_by_tg_message_id(session, message, references_msg_id)

    async def find_recent_partial_trade(
        self, session: AsyncSession, message: RawMessage
    ) -> Trade | None:
        rows = await self._query_trades_active_since(
            session,
            states=(TradeState.PARTIAL.value,),
            cutoff=message.sent_at - self._window,
        )
        return rows[0] if rows else None

    async def find_duplicate_trade(
        self, session: AsyncSession, message: RawMessage, fields: SignalFields
    ) -> Trade | None:
        if not self._has_duplicate_key(fields):
            return None
        rows = await self._query_trades_active_since(
            session,
            states=OPEN_FOR_MERGE_STATES,
            cutoff=message.sent_at - self._window,
            extra_filters=self._build_duplicate_filters(fields),
        )
        return rows[0] if rows else None

    async def find_partial_trades_idle_since(
        self, session: AsyncSession, now: datetime
    ) -> list[Trade]:
        """PARTIAL trades whose latest linked message is older than the assembly window."""
        last_activity = func.max(RawMessage.sent_at)
        result = await session.execute(
            select(Trade)
            .join(TradeMessage, TradeMessage.trade_id == Trade.id)
            .join(RawMessage, RawMessage.id == TradeMessage.message_id)
            .where(Trade.state == TradeState.PARTIAL.value)
            .group_by(Trade.id)
            .having(last_activity < now - self._window)
        )
        return list(result.scalars())

    async def _find_trade_by_tg_message_id(
        self, session: AsyncSession, message: RawMessage, tg_message_id: int | None
    ) -> Trade | None:
        if tg_message_id is None:
            return None
        result = await session.execute(
            select(Trade)
            .join(TradeMessage, TradeMessage.trade_id == Trade.id)
            .join(RawMessage, RawMessage.id == TradeMessage.message_id)
            .where(
                RawMessage.channel_id == message.channel_id,
                RawMessage.tg_message_id == tg_message_id,
            )
            .order_by(Trade.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _query_trades_active_since(
        self, session: AsyncSession, states: tuple[str, ...], cutoff, extra_filters=()
    ) -> list[Trade]:
        last_activity = func.max(RawMessage.sent_at)
        result = await session.execute(
            select(Trade)
            .join(TradeMessage, TradeMessage.trade_id == Trade.id)
            .join(RawMessage, RawMessage.id == TradeMessage.message_id)
            .where(Trade.state.in_(states), *extra_filters)
            .group_by(Trade.id)
            .having(last_activity >= cutoff)
            .order_by(last_activity.desc(), Trade.id.desc())
        )
        return list(result.scalars())

    def _has_duplicate_key(self, fields: SignalFields) -> bool:
        required = (
            fields.underlying,
            fields.strike,
            fields.option_type,
            fields.entry_low,
            fields.entry_high,
        )
        return all(value is not None for value in required)

    def _build_duplicate_filters(self, fields: SignalFields) -> list:
        filters = [
            Trade.underlying == fields.underlying,
            Trade.strike == fields.strike,
            Trade.option_type == fields.option_type.value,
            Trade.entry_low == fields.entry_low,
            Trade.entry_high == fields.entry_high,
        ]
        if fields.expiry is not None:
            filters.append(or_(Trade.expiry == fields.expiry, Trade.expiry.is_(None)))
        return filters
