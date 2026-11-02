from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.assembler.assembler import TradeAssembler
from app.assembler.matching import TradeMatcher
from app.db.message_store import RawMessageStore
from app.db.models import (
    MessageClassification,
    RawMessage,
    Trade,
    TradeEvent,
    TradeFieldHistory,
    TradeMessage,
)
from app.enums import ClassificationStatus, EventType, TradeState
from app.llm.classifier import MessageClassifier
from app.llm.prompts import PROMPT_VERSION
from tests.conftest import BASE_TIME, CHANNEL_ID, make_message
from tests.helpers import FixtureLlm, ingest_sample_messages

WINDOW = timedelta(minutes=30)
HOURS = 60


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def build_assembler(session_factory, clock: FakeClock) -> TradeAssembler:
    return TradeAssembler(session_factory, TradeMatcher(WINDOW), clock)


async def add_classified_message(
    session_factory,
    tg_message_id: int,
    minutes: float,
    message_type: str,
    fields: dict,
    reply_to: int | None = None,
    references: int | None = None,
) -> None:
    await RawMessageStore(session_factory).save_new_message(
        make_message(tg_message_id, f"text {tg_message_id}", minutes, reply_to)
    )
    async with session_factory.begin() as session:
        message_id = (
            await session.execute(
                select(RawMessage.id).where(RawMessage.tg_message_id == tg_message_id)
            )
        ).scalar_one()
        session.add(
            MessageClassification(
                message_id=message_id,
                message_type=message_type,
                confidence=0.9,
                fields_json={"targets": [], **fields},
                references_msg_id=references,
                model="test",
                prompt_version="v1",
                status=ClassificationStatus.OK.value,
            )
        )


async def load_all(session_factory, model) -> list:
    async with session_factory() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


async def load_trade_messages(session_factory) -> list[tuple[int, int, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(TradeMessage.trade_id, RawMessage.tg_message_id, TradeMessage.role)
            .join(RawMessage, RawMessage.id == TradeMessage.message_id)
            .order_by(RawMessage.tg_message_id)
        )
        return [tuple(row) for row in rows.all()]


SPLIT_NAME_AND_TARGET = {
    "underlying": "BANKNIFTY",
    "strike": 52000,
    "option_type": "CE",
    "targets": [250, 300],
}


async def test_split_message_sequence_assembles_one_complete_trade(session_factory):
    clock = FakeClock(BASE_TIME + timedelta(minutes=15))
    assembler = build_assembler(session_factory, clock)
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(session_factory, 102, 2, "SIGNAL_UPDATE", {"stop_loss": 180})
    await add_classified_message(
        session_factory, 103, 4, "ENTRY_TRIGGER", {"entry_low": 200, "entry_high": 205}
    )

    assert await assembler.assemble_pending() == 3

    [trade] = await load_all(session_factory, Trade)
    assert trade.state == TradeState.COMPLETE.value
    assert (trade.underlying, trade.strike, trade.option_type) == ("BANKNIFTY", 52000, "CE")
    assert (trade.entry_low, trade.entry_high, trade.stop_loss) == (200, 205, 180)
    assert trade.targets_json == [250, 300]
    assert trade.completed_at == clock.now
    assert await load_trade_messages(session_factory) == [
        (trade.id, 101, "ORIGIN"),
        (trade.id, 102, "UPDATE"),
        (trade.id, 103, "TRIGGER"),
    ]
    events = [e.event_type for e in await load_all(session_factory, TradeEvent)]
    assert events == [
        EventType.CAPTURED.value,
        EventType.FIELD_UPDATED.value,
        EventType.FIELD_UPDATED.value,
        EventType.FIELD_UPDATED.value,
        EventType.COMPLETE.value,
    ]


async def test_trade_stays_partial_until_every_required_field_is_present(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=5)))
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(session_factory, 102, 2, "SIGNAL_UPDATE", {"stop_loss": 180})

    await assembler.assemble_pending()

    [trade] = await load_all(session_factory, Trade)
    assert trade.state == TradeState.PARTIAL.value
    assert trade.completed_at is None


async def test_field_changes_are_recorded_in_history(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=5)))
    await add_classified_message(
        session_factory, 101, 0, "NEW_SIGNAL", {**SPLIT_NAME_AND_TARGET, "stop_loss": 180}
    )
    await add_classified_message(session_factory, 102, 2, "SIGNAL_UPDATE", {"stop_loss": 190})

    await assembler.assemble_pending()

    history = await load_all(session_factory, TradeFieldHistory)
    stop_loss_changes = [(h.old_value, h.new_value) for h in history if h.field == "stop_loss"]
    assert stop_loss_changes == [("null", "180.0"), ("180.0", "190.0")]


async def test_entry_trigger_only_fills_missing_fields_but_update_overwrites(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=5)))
    await add_classified_message(
        session_factory, 101, 0, "NEW_SIGNAL", {**SPLIT_NAME_AND_TARGET, "stop_loss": 180}
    )
    await add_classified_message(
        session_factory, 102, 1, "ENTRY_TRIGGER", {"entry_low": 200, "entry_high": 205, "stop_loss": 150}
    )
    await assembler.assemble_pending()
    [trade] = await load_all(session_factory, Trade)
    assert trade.stop_loss == 180  # trigger did not overwrite

    await add_classified_message(
        session_factory, 103, 2, "SIGNAL_UPDATE", {"stop_loss": 170}, reply_to=101
    )
    await assembler.assemble_pending()
    [trade] = await load_all(session_factory, Trade)
    assert trade.stop_loss == 170  # explicit update did


async def test_reply_link_beats_most_recent_partial_trade(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(session_factory, 201, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(
        session_factory, 202, 1, "NEW_SIGNAL",
        {"underlying": "NIFTY", "strike": 24500, "option_type": "PE", "targets": [150]},
    )
    await add_classified_message(
        session_factory, 203, 2, "SIGNAL_UPDATE", {"stop_loss": 180}, reply_to=201
    )

    await assembler.assemble_pending()

    first, second = await load_all(session_factory, Trade)
    assert first.stop_loss == 180
    assert second.stop_loss is None


async def test_llm_reference_is_used_when_there_is_no_reply(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(session_factory, 201, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(
        session_factory, 202, 1, "NEW_SIGNAL",
        {"underlying": "NIFTY", "strike": 24500, "option_type": "PE", "targets": [150]},
    )
    await add_classified_message(
        session_factory, 203, 2, "SIGNAL_UPDATE", {"stop_loss": 180}, references=201
    )

    await assembler.assemble_pending()

    first, second = await load_all(session_factory, Trade)
    assert first.stop_loss == 180
    assert second.stop_loss is None


async def test_update_after_the_window_does_not_attach_and_trade_expires(session_factory):
    clock = FakeClock(BASE_TIME + timedelta(minutes=45))
    assembler = build_assembler(session_factory, clock)
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(session_factory, 102, 31, "SIGNAL_UPDATE", {"stop_loss": 180})

    await assembler.assemble_pending()
    assert await assembler.expire_incomplete_trades() == 1

    [trade] = await load_all(session_factory, Trade)
    assert trade.state == TradeState.EXPIRED_INCOMPLETE.value
    assert trade.stop_loss is None
    events = [e.event_type for e in await load_all(session_factory, TradeEvent)]
    assert EventType.EXPIRED.value in events


async def test_partial_trade_inside_the_window_is_not_expired(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=20)))
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await assembler.assemble_pending()

    assert await assembler.expire_incomplete_trades() == 0


async def test_complete_trade_is_never_expired(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(hours=5)))
    await add_classified_message(
        session_factory, 101, 0, "NEW_SIGNAL",
        {**SPLIT_NAME_AND_TARGET, "entry_low": 200, "entry_high": 205, "stop_loss": 180},
    )
    await assembler.assemble_pending()

    assert await assembler.expire_incomplete_trades() == 0
    [trade] = await load_all(session_factory, Trade)
    assert trade.state == TradeState.COMPLETE.value


async def test_repeated_signal_in_window_is_the_same_trade(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    full_signal = {
        **SPLIT_NAME_AND_TARGET,
        "entry_low": 200,
        "entry_high": 205,
        "stop_loss": 180,
    }
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", full_signal)
    await add_classified_message(session_factory, 102, 5, "NEW_SIGNAL", full_signal)

    await assembler.assemble_pending()

    assert len(await load_all(session_factory, Trade)) == 1
    assert [role for _, _, role in await load_trade_messages(session_factory)] == ["ORIGIN", "UPDATE"]


async def test_same_signal_outside_the_window_is_a_new_trade(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(hours=2)))
    full_signal = {
        **SPLIT_NAME_AND_TARGET,
        "entry_low": 200,
        "entry_high": 205,
        "stop_loss": 180,
    }
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", full_signal)
    await add_classified_message(session_factory, 102, 45, "NEW_SIGNAL", full_signal)

    await assembler.assemble_pending()

    assert len(await load_all(session_factory, Trade)) == 2


async def test_assembling_twice_creates_nothing_new(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(session_factory, 102, 2, "SIGNAL_UPDATE", {"stop_loss": 180})

    await assembler.assemble_pending()
    assert await assembler.assemble_pending() == 0

    assert len(await load_all(session_factory, Trade)) == 1
    assert len(await load_trade_messages(session_factory)) == 2


async def test_update_without_any_trade_is_ignored(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=5)))
    await add_classified_message(session_factory, 101, 0, "SIGNAL_UPDATE", {"stop_loss": 180})

    await assembler.assemble_pending()

    assert await load_all(session_factory, Trade) == []


async def test_noise_and_exit_messages_create_no_trades(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=5)))
    await add_classified_message(session_factory, 101, 0, "NOISE", {})
    await add_classified_message(session_factory, 102, 1, "TARGET_HIT", {})
    await add_classified_message(session_factory, 103, 2, "EXIT", {})

    assert await assembler.assemble_pending() == 3
    assert await load_all(session_factory, Trade) == []


async def test_edited_new_signal_updates_its_trade_instead_of_creating_another(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await assembler.assemble_pending()

    async with session_factory.begin() as session:
        message = (await session.execute(select(RawMessage))).scalar_one()
        session.add(
            MessageClassification(
                message_id=message.id,
                message_type="NEW_SIGNAL",
                confidence=0.9,
                fields_json={**SPLIT_NAME_AND_TARGET, "strike": 52100, "targets": [250, 300]},
                model="test",
                prompt_version="v1",
                status=ClassificationStatus.OK.value,
            )
        )
    await assembler.assemble_pending()

    [trade] = await load_all(session_factory, Trade)
    assert trade.strike == 52100


async def test_active_trade_fields_are_frozen(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(
        session_factory, 101, 0, "NEW_SIGNAL",
        {**SPLIT_NAME_AND_TARGET, "entry_low": 200, "entry_high": 205, "stop_loss": 180},
    )
    await assembler.assemble_pending()
    async with session_factory.begin() as session:
        trade = (await session.execute(select(Trade))).scalar_one()
        trade.state = TradeState.ACTIVE.value
    await add_classified_message(
        session_factory, 102, 2, "SIGNAL_UPDATE", {"stop_loss": 150}, reply_to=101
    )

    await assembler.assemble_pending()

    [trade] = await load_all(session_factory, Trade)
    assert trade.stop_loss == 180


async def test_premium_update_is_linked_by_reply_without_changing_fields(session_factory):
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(minutes=15)))
    await add_classified_message(session_factory, 101, 0, "NEW_SIGNAL", SPLIT_NAME_AND_TARGET)
    await add_classified_message(
        session_factory, 102, 3, "PREMIUM_UPDATE", {"current_premium": 215}, reply_to=101
    )

    await assembler.assemble_pending()

    [trade] = await load_all(session_factory, Trade)
    assert trade.stop_loss is None
    assert (trade.id, 102, "PREMIUM") in await load_trade_messages(session_factory)


async def test_fixture_conversation_end_to_end(session_factory):
    store = RawMessageStore(session_factory)
    await ingest_sample_messages(store)
    classifier = MessageClassifier(session_factory, FixtureLlm(), "test", PROMPT_VERSION, 10)
    await classifier.classify_pending()
    assembler = build_assembler(session_factory, FakeClock(BASE_TIME + timedelta(hours=4)))

    await assembler.assemble_pending()
    await assembler.expire_incomplete_trades()

    banknifty, nifty = await load_all(session_factory, Trade)
    assert banknifty.state == TradeState.COMPLETE.value
    assert (banknifty.entry_low, banknifty.stop_loss, banknifty.targets_json) == (200, 180, [250, 300])
    assert nifty.state == TradeState.COMPLETE.value
    assert nifty.targets_json == [150, 170, 190]
    linked = [(tg, role) for trade_id, tg, role in await load_trade_messages(session_factory) if trade_id == banknifty.id]
    assert linked == [(101, "ORIGIN"), (102, "UPDATE"), (103, "TRIGGER"), (104, "PREMIUM")]
