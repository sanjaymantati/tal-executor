"""Builders that assemble the application's objects from settings."""

from datetime import timedelta
from pathlib import Path

from telethon import TelegramClient

from app.assembler.assembler import TradeAssembler
from app.assembler.matching import TradeMatcher
from app.broker.base import BrokerClient
from app.config import Settings
from app.db.message_store import RawMessageStore
from app.db.session import SessionFactory
from app.execution.executor import PaperTradeExecutor
from app.execution.ledger import PaperTradeLedger
from app.llm.classifier import MessageClassifier
from app.llm.factory import build_llm_client
from app.llm.prompts import PROMPT_VERSION
from app.status import PipelineStatusStore
from app.telegram.backfill import ChannelBackfiller
from app.telegram.ingestor import ReconnectPolicy, TelegramIngestor
from app.timeutil import utc_now

RECONNECT_INITIAL_DELAY_SEC = 5.0
RECONNECT_MAX_DELAY_SEC = 300.0


def build_status_store(settings: Settings) -> PipelineStatusStore:
    return PipelineStatusStore(Path(settings.status_file), utc_now)


def build_ingestor(
    settings: Settings,
    session_factory: SessionFactory,
    client: TelegramClient,
    status_store: PipelineStatusStore,
) -> TelegramIngestor:
    store = RawMessageStore(session_factory)
    backfiller = ChannelBackfiller(store, settings.backfill_initial_limit)
    policy = ReconnectPolicy(RECONNECT_INITIAL_DELAY_SEC, RECONNECT_MAX_DELAY_SEC)
    return TelegramIngestor(
        client, store, backfiller, settings.tg_channel, settings.tg_phone, policy, status_store
    )


def build_classifier(settings: Settings, session_factory: SessionFactory) -> MessageClassifier:
    llm = build_llm_client(settings)
    return MessageClassifier(
        session_factory,
        llm,
        settings.openai_model,
        PROMPT_VERSION,
        settings.classifier_context_size,
    )


def build_assembler(settings: Settings, session_factory: SessionFactory) -> TradeAssembler:
    matcher = TradeMatcher(timedelta(minutes=settings.assembly_window_min))
    return TradeAssembler(session_factory, matcher, utc_now)


def build_executor(
    settings: Settings, session_factory: SessionFactory, broker: BrokerClient
) -> PaperTradeExecutor:
    ledger = PaperTradeLedger(session_factory)
    return PaperTradeExecutor(ledger, broker, settings.to_execution_config(), utc_now)
