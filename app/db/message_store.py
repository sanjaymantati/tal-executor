from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import MessageClassification, RawMessage
from app.db.session import SessionFactory
from app.enums import ClassificationStatus


@dataclass(frozen=True)
class IncomingMessage:
    channel_id: int
    tg_message_id: int
    reply_to_msg_id: int | None
    text: str
    sent_at: datetime
    edited_at: datetime | None
    raw_json: dict | None


class RawMessageStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def save_new_message(self, incoming: IncomingMessage) -> bool:
        """Insert-or-ignore. Returns True when a row was actually inserted."""
        async with self._session_factory() as session:
            session.add(self._build_row(incoming))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def apply_edit(self, incoming: IncomingMessage) -> None:
        async with self._session_factory.begin() as session:
            row = await self._find_row(session, incoming)
            if row is None:
                session.add(self._build_row(incoming))
                return
            text_changed = row.text != incoming.text
            row.text = incoming.text
            row.edited_at = incoming.edited_at
            row.raw_json = incoming.raw_json
            if text_changed:
                await self._mark_classifications_stale(session, row.id)

    async def find_last_tg_message_id(self, channel_id: int) -> int | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.max(RawMessage.tg_message_id)).where(
                    RawMessage.channel_id == channel_id
                )
            )
            return result.scalar_one()

    async def _find_row(self, session, incoming: IncomingMessage) -> RawMessage | None:
        result = await session.execute(
            select(RawMessage).where(
                RawMessage.channel_id == incoming.channel_id,
                RawMessage.tg_message_id == incoming.tg_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def _mark_classifications_stale(self, session, message_id: int) -> None:
        await session.execute(
            update(MessageClassification)
            .where(
                MessageClassification.message_id == message_id,
                MessageClassification.status != ClassificationStatus.STALE.value,
            )
            .values(status=ClassificationStatus.STALE.value)
        )

    def _build_row(self, incoming: IncomingMessage) -> RawMessage:
        return RawMessage(
            channel_id=incoming.channel_id,
            tg_message_id=incoming.tg_message_id,
            reply_to_msg_id=incoming.reply_to_msg_id,
            text=incoming.text,
            sent_at=incoming.sent_at,
            edited_at=incoming.edited_at,
            raw_json=incoming.raw_json,
        )
