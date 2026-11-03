"""Manual evaluation: real OpenAI calls against a hand-labelled fixture file.

    python -m scripts.eval_classifier [path/to/labelled.json]

The file uses the same format as tests/fixtures/sample_messages.json. Prints per-class precision,
recall and the confusion of NEW_SIGNAL / SIGNAL_UPDATE / ENTRY_TRIGGER (the Phase 1 priorities).
Uses an isolated temporary SQLite database; your real database is untouched.
"""

import asyncio
import json
import sys
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.db.message_store import IncomingMessage, RawMessageStore
from app.db.models import Base, MessageClassification, RawMessage
from app.db.session import create_engine, create_session_factory
from app.llm.classifier import MessageClassifier
from app.llm.factory import build_llm_client
from app.llm.prompts import PROMPT_VERSION
from tests.conftest import BASE_TIME, CHANNEL_ID

DEFAULT_FIXTURE = Path("tests/fixtures/sample_messages.json")
PRIORITY_TYPES = ("NEW_SIGNAL", "SIGNAL_UPDATE", "ENTRY_TRIGGER")


async def evaluate(fixture_path: Path) -> None:
    settings = Settings()
    samples = json.loads(fixture_path.read_text())["messages"]
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(f"sqlite+aiosqlite:///{directory}/eval.db")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        await load_samples(RawMessageStore(session_factory), samples)
        llm = build_llm_client(settings)
        classifier = MessageClassifier(
            session_factory,
            llm,
            settings.openai_model,
            PROMPT_VERSION,
            settings.classifier_context_size,
        )
        await classifier.classify_pending()
        predictions = await read_predictions(session_factory)
        await engine.dispose()
    print_report(samples, predictions)


async def load_samples(store: RawMessageStore, samples: list[dict]) -> None:
    for sample in samples:
        await store.save_new_message(
            IncomingMessage(
                channel_id=CHANNEL_ID,
                tg_message_id=sample["tg_message_id"],
                reply_to_msg_id=sample["reply_to_msg_id"],
                text=sample["text"],
                sent_at=BASE_TIME + timedelta(minutes=sample["minutes"]),
                edited_at=None,
                raw_json=None,
            )
        )


async def read_predictions(session_factory) -> dict[int, str | None]:
    async with session_factory() as session:
        rows = await session.execute(
            select(RawMessage.tg_message_id, MessageClassification.message_type).join(
                MessageClassification, MessageClassification.message_id == RawMessage.id
            )
        )
        return {tg_id: message_type for tg_id, message_type in rows.all()}


def print_report(samples: list[dict], predictions: dict[int, str | None]) -> None:
    pairs = [(s["label"]["message_type"], predictions.get(s["tg_message_id"])) for s in samples]
    correct = sum(1 for expected, predicted in pairs if expected == predicted)
    print(f"Overall accuracy: {correct}/{len(pairs)} = {correct / len(pairs):.1%}")
    for message_type in PRIORITY_TYPES:
        print_class_metrics(message_type, pairs)
    print("Confusion (expected -> predicted):")
    for (expected, predicted), count in sorted(Counter(pairs).items(), key=str):
        marker = "" if expected == predicted else "  <-- wrong"
        print(f"  {expected} -> {predicted}: {count}{marker}")


def print_class_metrics(message_type: str, pairs: list[tuple]) -> None:
    true_positive = sum(1 for e, p in pairs if e == p == message_type)
    predicted = sum(1 for _, p in pairs if p == message_type)
    expected = sum(1 for e, _ in pairs if e == message_type)
    precision = f"{true_positive / predicted:.1%}" if predicted else "n/a"
    recall = f"{true_positive / expected:.1%}" if expected else "n/a"
    print(f"{message_type}: precision={precision} recall={recall} (n={expected})")


if __name__ == "__main__":
    asyncio.run(evaluate(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE))
