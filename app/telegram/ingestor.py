import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
from telethon import events, utils

from app.db.message_store import RawMessageStore
from app.status import PipelineStatusStore
from app.telegram.backfill import ChannelBackfiller
from app.telegram.conversion import to_incoming_message

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_delay_sec: float
    max_delay_sec: float

    def next_delay(self, current_delay_sec: float) -> float:
        return min(current_delay_sec * 2, self.max_delay_sec)


class TelegramIngestor:
    """Read-only listener for a single channel. Never sends anything."""

    def __init__(
        self,
        client: Any,
        store: RawMessageStore,
        backfiller: ChannelBackfiller,
        channel_reference: str,
        phone: str,
        reconnect_policy: ReconnectPolicy,
        status_store: PipelineStatusStore,
    ) -> None:
        self._client = client
        self._store = store
        self._backfiller = backfiller
        self._channel_reference = channel_reference
        self._phone = phone
        self._reconnect_policy = reconnect_policy
        self._status_store = status_store
        self._entity: Any = None
        self._channel_id: int | None = None
        self._channel_title: str | None = None

    async def run_forever(self) -> None:
        delay = self._reconnect_policy.initial_delay_sec
        while True:
            try:
                await self._connect_and_listen()
                delay = self._reconnect_policy.initial_delay_sec
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram_connection_failed", retry_in_sec=delay)
            self._publish_status("disconnected")
            await asyncio.sleep(delay)
            delay = self._reconnect_policy.next_delay(delay)

    async def _connect_and_listen(self) -> None:
        self._publish_status("connecting")
        await self._client.start(phone=self._phone)
        if self._entity is None:
            await self._resolve_channel()
            self._register_handlers()
        await self._backfiller.backfill(self._client, self._entity, self._channel_id)
        log.info("telegram_listening", channel_id=self._channel_id, channel=self._channel_title)
        self._publish_status("listening")
        await self._client.run_until_disconnected()
        log.warning("telegram_disconnected")

    def _publish_status(self, state: str) -> None:
        try:
            self._status_store.update_telegram(state, self._channel_id, self._channel_title)
        except OSError:
            log.exception("status_publish_failed")

    async def _resolve_channel(self) -> None:
        reference = parse_channel_reference(self._channel_reference)
        try:
            self._entity = await self._client.get_entity(reference)
        except ValueError:
            await self._client.get_dialogs()
            self._entity = await self._client.get_entity(reference)
        self._channel_id = utils.get_peer_id(self._entity)
        self._channel_title = utils.get_display_name(self._entity)

    def _register_handlers(self) -> None:
        self._client.add_event_handler(self._on_new_message, events.NewMessage(chats=self._entity))
        self._client.add_event_handler(
            self._on_message_edited, events.MessageEdited(chats=self._entity)
        )

    async def _on_new_message(self, event: Any) -> None:
        try:
            incoming = to_incoming_message(event.message, self._channel_id)
            inserted = await self._store.save_new_message(incoming)
            log.info("message_captured", tg_message_id=incoming.tg_message_id, inserted=inserted)
        except Exception:
            log.exception("new_message_handling_failed")

    async def _on_message_edited(self, event: Any) -> None:
        try:
            incoming = to_incoming_message(event.message, self._channel_id)
            await self._store.apply_edit(incoming)
            log.info("message_edit_captured", tg_message_id=incoming.tg_message_id)
        except Exception:
            log.exception("edited_message_handling_failed")


def parse_channel_reference(reference: str) -> int | str:
    try:
        return int(reference)
    except ValueError:
        return reference
