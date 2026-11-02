import json
import re
from datetime import timedelta
from pathlib import Path

from app.db.message_store import IncomingMessage, RawMessageStore
from app.llm.client import LlmResponse
from tests.conftest import BASE_TIME, CHANNEL_ID

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_messages.json"
TARGET_ID_PATTERN = re.compile(r"MESSAGE TO CLASSIFY:\n\[id=(\d+)")


def load_sample_messages() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())["messages"]


async def ingest_sample_messages(store: RawMessageStore) -> None:
    for sample in load_sample_messages():
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


class FixtureLlm:
    """Answers with the hand label of whichever fixture message is being classified."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._labels = {s["tg_message_id"]: s["label"] for s in load_sample_messages()}

    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        self.prompts.append(user_prompt)
        tg_message_id = int(TARGET_ID_PATTERN.search(user_prompt).group(1))
        return LlmResponse(text=json.dumps(self._labels[tg_message_id]), total_tokens=100)


class ScriptedLlm:
    """Returns pre-baked raw responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete_json(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text=self._responses.pop(0), total_tokens=10)
