import json
import time
from dataclasses import dataclass

import structlog
from pydantic import ValidationError
from sqlalchemy import select

from app.db.models import MessageClassification, RawMessage
from app.db.session import SessionFactory
from app.enums import ClassificationStatus, MessageType
from app.llm.client import LlmClient
from app.llm.prompts import SYSTEM_PROMPT, PromptMessage, build_user_prompt
from app.llm.schemas import ClassificationResult, SignalFields

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = 2  # first call plus one retry on invalid output
FINISHED_STATUSES = (ClassificationStatus.OK.value, ClassificationStatus.FAILED.value)
NO_LLM_MODEL = "none"


@dataclass(frozen=True)
class ClassificationOutcome:
    status: ClassificationStatus
    result: ClassificationResult | None
    raw_response: str | None
    tokens: int | None
    latency_ms: int | None
    model: str


class MessageClassifier:
    def __init__(
        self,
        session_factory: SessionFactory,
        llm: LlmClient,
        model_name: str,
        prompt_version: str,
        context_size: int,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._context_size = context_size

    async def classify_pending(self) -> int:
        message_ids = await self._list_pending_message_ids()
        for message_id in message_ids:
            await self._classify_message(message_id)
        return len(message_ids)

    async def _list_pending_message_ids(self) -> list[int]:
        finished = select(MessageClassification.id).where(
            MessageClassification.message_id == RawMessage.id,
            MessageClassification.status.in_(FINISHED_STATUSES),
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(RawMessage.id)
                .where(~finished.exists())
                .order_by(RawMessage.tg_message_id, RawMessage.id)
            )
            return list(result.scalars())

    async def _classify_message(self, message_id: int) -> None:
        async with self._session_factory() as session:
            message = await session.get(RawMessage, message_id)
            prompt = await self._build_prompt(session, message)
        outcome = await self._obtain_outcome(message.text, prompt)
        await self._store_outcome(message, outcome)

    async def _build_prompt(self, session, message: RawMessage) -> str:
        replied_to = await self._load_replied_to(session, message)
        context = await self._load_context(session, message)
        return build_user_prompt(to_prompt_message(message), replied_to, context)

    async def _load_replied_to(self, session, message: RawMessage) -> PromptMessage | None:
        if message.reply_to_msg_id is None:
            return None
        result = await session.execute(
            select(RawMessage).where(
                RawMessage.channel_id == message.channel_id,
                RawMessage.tg_message_id == message.reply_to_msg_id,
            )
        )
        replied_to = result.scalar_one_or_none()
        return to_prompt_message(replied_to) if replied_to else None

    async def _load_context(self, session, message: RawMessage) -> list[PromptMessage]:
        result = await session.execute(
            select(RawMessage)
            .where(
                RawMessage.channel_id == message.channel_id,
                RawMessage.tg_message_id < message.tg_message_id,
            )
            .order_by(RawMessage.tg_message_id.desc())
            .limit(self._context_size)
        )
        newest_first = list(result.scalars())
        return [to_prompt_message(m) for m in reversed(newest_first)]

    async def _obtain_outcome(self, text: str, prompt: str) -> ClassificationOutcome:
        if not text.strip():
            return self._build_empty_message_outcome()
        return await self._call_llm_with_retry(prompt)

    def _build_empty_message_outcome(self) -> ClassificationOutcome:
        result = ClassificationResult(
            message_type=MessageType.NOISE, confidence=1.0, fields=SignalFields()
        )
        return ClassificationOutcome(
            status=ClassificationStatus.OK,
            result=result,
            raw_response=None,
            tokens=None,
            latency_ms=None,
            model=NO_LLM_MODEL,
        )

    async def _call_llm_with_retry(self, prompt: str) -> ClassificationOutcome:
        total_tokens = 0
        raw_response = None
        latency_ms = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            response = await self._llm.complete_json(SYSTEM_PROMPT, prompt)
            latency_ms = int((time.perf_counter() - started) * 1000)
            total_tokens += response.total_tokens or 0
            raw_response = response.text
            result = parse_classification(response.text)
            if result is not None:
                return self._build_outcome(
                    ClassificationStatus.OK, result, raw_response, total_tokens, latency_ms
                )
            log.warning("classification_invalid_output", attempt=attempt)
        return self._build_outcome(
            ClassificationStatus.FAILED, None, raw_response, total_tokens, latency_ms
        )

    def _build_outcome(
        self,
        status: ClassificationStatus,
        result: ClassificationResult | None,
        raw_response: str | None,
        tokens: int,
        latency_ms: int | None,
    ) -> ClassificationOutcome:
        return ClassificationOutcome(
            status=status,
            result=result,
            raw_response=raw_response,
            tokens=tokens,
            latency_ms=latency_ms,
            model=self._model_name,
        )

    async def _store_outcome(self, message: RawMessage, outcome: ClassificationOutcome) -> None:
        async with self._session_factory.begin() as session:
            current = await session.get(RawMessage, message.id)
            if current.text != message.text:
                log.info("classification_discarded_message_edited", message_id=message.id)
                return
            session.add(self._build_row(message.id, outcome))
        log.info(
            "message_classified",
            message_id=message.id,
            status=outcome.status.value,
            message_type=outcome.result.message_type.value if outcome.result else None,
        )

    def _build_row(self, message_id: int, outcome: ClassificationOutcome) -> MessageClassification:
        result = outcome.result
        return MessageClassification(
            message_id=message_id,
            message_type=result.message_type.value if result else None,
            confidence=result.confidence if result else None,
            fields_json=result.fields.model_dump(mode="json") if result else None,
            references_msg_id=result.references_msg_id if result else None,
            model=outcome.model,
            prompt_version=self._prompt_version,
            raw_response=outcome.raw_response,
            tokens=outcome.tokens,
            latency_ms=outcome.latency_ms,
            status=outcome.status.value,
        )


def parse_classification(raw_text: str) -> ClassificationResult | None:
    try:
        return ClassificationResult.model_validate(json.loads(raw_text))
    except (json.JSONDecodeError, ValidationError):
        return None


def to_prompt_message(message: RawMessage) -> PromptMessage:
    return PromptMessage(
        tg_message_id=message.tg_message_id, sent_at=message.sent_at, text=message.text
    )
