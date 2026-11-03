import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.message_store import RawMessageStore
from app.db.models import MessageClassification, RawMessage
from app.enums import ClassificationStatus, MessageType
from app.llm.classifier import MessageClassifier
from app.llm.prompts import PROMPT_VERSION
from app.llm.schemas import ClassificationResult, SignalFields
from tests.conftest import make_message
from tests.helpers import FixtureLlm, ScriptedLlm, ingest_sample_messages, load_sample_messages

VALID_NOISE = json.dumps(
    {"message_type": "NOISE", "confidence": 0.9, "fields": {}, "references_msg_id": None}
)


def build_classifier(session_factory, llm, context_size: int = 10) -> MessageClassifier:
    return MessageClassifier(session_factory, llm, "test-model", PROMPT_VERSION, context_size)


async def load_classifications(session_factory) -> list[MessageClassification]:
    async with session_factory() as session:
        result = await session.execute(
            select(MessageClassification).order_by(MessageClassification.id)
        )
        return list(result.scalars())


async def test_fixture_messages_are_classified_as_labelled(session_factory):
    await ingest_sample_messages(RawMessageStore(session_factory))
    classifier = build_classifier(session_factory, FixtureLlm())

    assert await classifier.classify_pending() == len(load_sample_messages())

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(RawMessage.tg_message_id, MessageClassification)
                .join(MessageClassification, MessageClassification.message_id == RawMessage.id)
            )
        ).all()
    labels = {s["tg_message_id"]: s["label"] for s in load_sample_messages()}
    assert len(rows) == len(labels)
    for tg_message_id, classification in rows:
        assert classification.message_type == labels[tg_message_id]["message_type"]
        assert classification.status == ClassificationStatus.OK.value
        assert classification.model == "test-model"
        assert classification.prompt_version == PROMPT_VERSION
        assert classification.tokens == 100
        assert classification.raw_response
        assert classification.latency_ms is not None


async def test_classifying_twice_does_not_duplicate_work(session_factory):
    await ingest_sample_messages(RawMessageStore(session_factory))
    llm = FixtureLlm()
    classifier = build_classifier(session_factory, llm)

    await classifier.classify_pending()
    calls_after_first_pass = len(llm.prompts)
    assert await classifier.classify_pending() == 0
    assert len(llm.prompts) == calls_after_first_pass


async def test_prompt_includes_reply_target_and_previous_messages(session_factory):
    await ingest_sample_messages(RawMessageStore(session_factory))
    llm = FixtureLlm()
    await build_classifier(session_factory, llm, context_size=2).classify_pending()

    prompt_for_104 = next(p for p in llm.prompts if "[id=104" in p.split("MESSAGE TO CLASSIFY:")[1])
    replied_section = prompt_for_104.split("REPLIED-TO MESSAGE:")[1].split("MESSAGE TO CLASSIFY:")[0]
    context_section = prompt_for_104.split("CONTEXT")[1].split("REPLIED-TO MESSAGE:")[0]
    assert "BUY ABOVE 200-205" in replied_section
    assert "[id=103" in context_section and "[id=102" in context_section
    assert "[id=101" not in context_section  # only the last N messages


async def test_invalid_json_is_retried_once_then_succeeds(session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(1, "hello", 0, None))
    llm = ScriptedLlm(["not json at all", VALID_NOISE])

    await build_classifier(session_factory, llm).classify_pending()

    [classification] = await load_classifications(session_factory)
    assert llm.calls == 2
    assert classification.status == ClassificationStatus.OK.value
    assert classification.tokens == 20


async def test_two_invalid_responses_mark_failed_and_processing_continues(session_factory):
    store = RawMessageStore(session_factory)
    await store.save_new_message(make_message(1, "first", 0, None))
    await store.save_new_message(make_message(2, "second", 1, None))
    bad_type = json.dumps({"message_type": "BOGUS", "confidence": 0.5, "fields": {}})
    llm = ScriptedLlm(["{oops", bad_type, VALID_NOISE])

    await build_classifier(session_factory, llm).classify_pending()

    failed, ok = await load_classifications(session_factory)
    assert failed.status == ClassificationStatus.FAILED.value
    assert failed.raw_response == bad_type
    assert failed.message_type is None
    assert ok.status == ClassificationStatus.OK.value


async def test_failed_messages_are_not_retried_forever(session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(1, "x", 0, None))
    llm = ScriptedLlm(["bad", "bad"])
    classifier = build_classifier(session_factory, llm)

    await classifier.classify_pending()
    assert await classifier.classify_pending() == 0
    assert llm.calls == 2


async def test_empty_text_is_noise_without_calling_the_llm(session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(1, "   ", 0, None))
    llm = ScriptedLlm([])

    await build_classifier(session_factory, llm).classify_pending()

    [classification] = await load_classifications(session_factory)
    assert llm.calls == 0
    assert classification.message_type == MessageType.NOISE.value


async def test_numeric_reply_is_a_premium_update_without_calling_the_llm(session_factory):
    store = RawMessageStore(session_factory)
    await store.save_new_message(make_message(2, "215", 1, 1))
    await store.save_new_message(make_message(3, "  42.5  ", 2, 1))
    llm = ScriptedLlm([])

    await build_classifier(session_factory, llm).classify_pending()

    first, second = await load_classifications(session_factory)
    assert llm.calls == 0
    assert first.message_type == MessageType.PREMIUM_UPDATE.value
    assert first.fields_json["current_premium"] == 215
    assert first.references_msg_id == 1
    assert first.confidence == 1
    assert first.model == "none"
    assert first.tokens is None
    assert second.fields_json["current_premium"] == 42.5
    assert second.references_msg_id == 1


async def test_bare_number_without_a_reply_still_calls_the_llm(session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(1, "215", 0, None))
    llm = ScriptedLlm([VALID_NOISE])

    await build_classifier(session_factory, llm).classify_pending()

    assert llm.calls == 1


async def test_reply_that_is_not_only_a_number_still_calls_the_llm(session_factory):
    await RawMessageStore(session_factory).save_new_message(make_message(1, "SL 180", 0, 9))
    llm = ScriptedLlm([VALID_NOISE])

    await build_classifier(session_factory, llm).classify_pending()

    assert llm.calls == 1


async def test_edited_message_is_reclassified(session_factory):
    store = RawMessageStore(session_factory)
    await store.save_new_message(make_message(1, "first version", 0, None))
    llm = ScriptedLlm([VALID_NOISE, VALID_NOISE])
    classifier = build_classifier(session_factory, llm)
    await classifier.classify_pending()

    await store.apply_edit(make_message(1, "second version", 0, None))
    assert await classifier.classify_pending() == 1

    statuses = [c.status for c in await load_classifications(session_factory)]
    assert statuses == [ClassificationStatus.STALE.value, ClassificationStatus.OK.value]


def test_single_entry_price_becomes_equal_bounds():
    assert SignalFields(entry_low=140).entry_high == 140
    fields = SignalFields(entry_high=145, entry_low=140)
    assert (fields.entry_low, fields.entry_high) == (140, 145)


def test_missing_targets_become_an_empty_list():
    assert SignalFields.model_validate({"targets": None}).targets == []


def test_underlying_is_normalised():
    assert SignalFields(underlying="bank nifty").underlying == "BANKNIFTY"
    assert SignalFields(underlying="Nifty 50").underlying == "NIFTY"


def test_confidence_outside_unit_range_is_rejected():
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate(
            {"message_type": "NOISE", "confidence": 1.5, "fields": {}}
        )
