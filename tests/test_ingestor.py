from datetime import timedelta

from sqlalchemy import func, select

from app.db.message_store import RawMessageStore
from app.db.models import RawMessage
from app.telegram.backfill import ChannelBackfiller
from app.telegram.conversion import to_incoming_message
from app.telegram.ingestor import parse_channel_reference
from tests.conftest import BASE_TIME, CHANNEL_ID


class FakeTelegramMessage:
    def __init__(self, message_id: int, text: str, minutes: int, reply_to: int | None = None) -> None:
        self.id = message_id
        self.message = text
        self.date = BASE_TIME + timedelta(minutes=minutes)
        self.edit_date = None
        self.reply_to_msg_id = reply_to

    def to_dict(self) -> dict:
        return {"id": self.id, "date": self.date, "message": self.message}


class FakeTelegramClient:
    def __init__(self, history: list[FakeTelegramMessage]) -> None:
        self._history = history
        self.requests: list[dict] = []

    async def iter_messages(self, entity, min_id=None, reverse=False, limit=None):
        self.requests.append({"min_id": min_id, "reverse": reverse, "limit": limit})
        messages = sorted(self._history, key=lambda m: m.id, reverse=not reverse)
        if min_id is not None:
            messages = [m for m in messages if m.id > min_id]
        for message in messages[:limit]:
            yield message


async def count_messages(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count(RawMessage.id)))).scalar_one()


def test_telegram_message_is_converted_with_reply_reference_and_raw_json():
    incoming = to_incoming_message(FakeTelegramMessage(5, "SL 180", 3, reply_to=4), CHANNEL_ID)

    assert incoming.reply_to_msg_id == 4
    assert incoming.sent_at == BASE_TIME + timedelta(minutes=3)
    assert incoming.raw_json["id"] == 5
    assert isinstance(incoming.raw_json["date"], str)  # datetimes made JSON-safe


def test_numeric_channel_reference_is_parsed_but_usernames_are_kept():
    assert parse_channel_reference("-1001234567890") == -1001234567890
    assert parse_channel_reference("@signals") == "@signals"


async def test_backfill_only_fetches_messages_after_the_last_stored_one(session_factory):
    store = RawMessageStore(session_factory)
    history = [FakeTelegramMessage(i, f"m{i}", i) for i in range(1, 8)]
    for message in history[:4]:
        await store.save_new_message(to_incoming_message(message, CHANNEL_ID))
    client = FakeTelegramClient(history)

    inserted = await ChannelBackfiller(store, initial_limit=0).backfill(client, None, CHANNEL_ID)

    assert inserted == 3
    assert client.requests == [{"min_id": 4, "reverse": True, "limit": None}]
    assert await count_messages(session_factory) == 7


async def test_backfill_twice_creates_no_duplicates(session_factory):
    store = RawMessageStore(session_factory)
    client = FakeTelegramClient([FakeTelegramMessage(i, f"m{i}", i) for i in range(1, 4)])
    backfiller = ChannelBackfiller(store, initial_limit=10)

    assert await backfiller.backfill(client, None, CHANNEL_ID) == 3
    assert await backfiller.backfill(client, None, CHANNEL_ID) == 0
    assert await count_messages(session_factory) == 3


async def test_first_run_does_not_pull_history_unless_asked(session_factory):
    client = FakeTelegramClient([FakeTelegramMessage(1, "old", 1)])

    inserted = await ChannelBackfiller(RawMessageStore(session_factory), 0).backfill(
        client, None, CHANNEL_ID
    )

    assert inserted == 0
    assert client.requests == []
