from datetime import timedelta

from sqlalchemy import func, select

from app.db.message_store import RawMessageStore
from app.db.models import MessageClassification, RawMessage
from app.enums import ClassificationStatus
from tests.conftest import BASE_TIME, CHANNEL_ID, make_message


async def count_messages(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count(RawMessage.id)))).scalar_one()


async def test_replaying_the_same_messages_creates_no_duplicates(session_factory):
    store = RawMessageStore(session_factory)
    messages = [make_message(i, f"msg {i}", i, None) for i in range(1, 6)]

    first_pass = [await store.save_new_message(m) for m in messages]
    second_pass = [await store.save_new_message(m) for m in messages]

    assert all(first_pass)
    assert not any(second_pass)
    assert await count_messages(session_factory) == 5


async def test_last_message_id_supports_backfill_after_restart(session_factory):
    store = RawMessageStore(session_factory)
    assert await store.find_last_tg_message_id(CHANNEL_ID) is None

    await store.save_new_message(make_message(10, "a", 1, None))
    await store.save_new_message(make_message(12, "b", 2, None))

    assert await store.find_last_tg_message_id(CHANNEL_ID) == 12


async def test_edit_updates_text_and_marks_classification_stale(session_factory):
    store = RawMessageStore(session_factory)
    original = make_message(1, "BANKNIFTY 52000 CE", 1, None)
    await store.save_new_message(original)
    await add_ok_classification(session_factory)

    edited = make_message(1, "BANKNIFTY 52100 CE", 1, None)
    edit_time = BASE_TIME + timedelta(minutes=3)
    await store.apply_edit(
        type(edited)(**{**edited.__dict__, "edited_at": edit_time})
    )

    async with session_factory() as session:
        row = (await session.execute(select(RawMessage))).scalar_one()
        classification = (await session.execute(select(MessageClassification))).scalar_one()
    assert row.text == "BANKNIFTY 52100 CE"
    assert row.edited_at == edit_time
    assert classification.status == ClassificationStatus.STALE.value


async def test_edit_of_unknown_message_is_stored(session_factory):
    store = RawMessageStore(session_factory)
    await store.apply_edit(make_message(7, "late edit", 1, None))
    assert await count_messages(session_factory) == 1


async def add_ok_classification(session_factory) -> None:
    async with session_factory.begin() as session:
        message_id = (await session.execute(select(RawMessage.id))).scalar_one()
        session.add(
            MessageClassification(
                message_id=message_id,
                message_type="NEW_SIGNAL",
                model="test",
                prompt_version="v1",
                status=ClassificationStatus.OK.value,
            )
        )
