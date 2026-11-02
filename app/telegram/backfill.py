from typing import Any

import structlog

from app.db.message_store import RawMessageStore
from app.telegram.conversion import to_incoming_message

log = structlog.get_logger(__name__)


class ChannelBackfiller:
    """Fetches messages missed while the process was down."""

    def __init__(self, store: RawMessageStore, initial_limit: int) -> None:
        self._store = store
        self._initial_limit = initial_limit

    async def backfill(self, client: Any, entity: Any, channel_id: int) -> int:
        last_id = await self._store.find_last_tg_message_id(channel_id)
        messages = await self._fetch_missed_messages(client, entity, last_id)
        inserted = 0
        for message in messages:
            if await self._store_message(message, channel_id):
                inserted += 1
        log.info("backfill_done", fetched=len(messages), inserted=inserted)
        return inserted

    async def _fetch_missed_messages(self, client: Any, entity: Any, last_id: int | None) -> list:
        if last_id is None:
            return await self._fetch_initial_messages(client, entity)
        return [m async for m in client.iter_messages(entity, min_id=last_id, reverse=True)]

    async def _fetch_initial_messages(self, client: Any, entity: Any) -> list:
        if self._initial_limit <= 0:
            return []
        newest_first = [m async for m in client.iter_messages(entity, limit=self._initial_limit)]
        return list(reversed(newest_first))

    async def _store_message(self, message: Any, channel_id: int) -> bool:
        try:
            return await self._store.save_new_message(to_incoming_message(message, channel_id))
        except Exception:
            log.exception("backfill_message_failed", tg_message_id=getattr(message, "id", None))
            return False
