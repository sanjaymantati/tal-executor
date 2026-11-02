from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.enums import ClassificationStatus, PaperTradeStatus, TradeState


class UtcDateTime(TypeDecorator):
    """Stores UTC; always returns timezone-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed in the database")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utc_now_default() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (UniqueConstraint("channel_id", "tg_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(UtcDateTime)
    edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now_default)


class MessageClassification(Base):
    __tablename__ = "message_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    message_type: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    fields_json: Mapped[dict | None] = mapped_column(JSON)
    references_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    raw_response: Mapped[str | None] = mapped_column(Text)
    tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=ClassificationStatus.OK.value)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now_default)
    # Not in the original schema: marks that the assembler consumed this row (stage idempotency).
    assembled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    underlying: Mapped[str | None] = mapped_column(String(32))
    expiry: Mapped[str | None] = mapped_column(String(32))
    strike: Mapped[float | None] = mapped_column(Float)
    option_type: Mapped[str | None] = mapped_column(String(2))
    instrument_key: Mapped[str | None] = mapped_column(String(64))
    entry_low: Mapped[float | None] = mapped_column(Float)
    entry_high: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    targets_json: Mapped[list | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default=TradeState.PARTIAL.value, index=True)
    first_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now_default)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now_default, onupdate=utc_now_default
    )


class TradeMessage(Base):
    __tablename__ = "trade_messages"

    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))


class TradeFieldHistory(Base):
    __tablename__ = "trade_field_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    field: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"))
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now_default)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), unique=True)
    entry_price: Mapped[float | None] = mapped_column(Float)
    entry_time: Mapped[datetime | None] = mapped_column(UtcDateTime)
    qty: Mapped[int | None] = mapped_column(Integer)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(UtcDateTime)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    pnl: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    fees: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default=PaperTradeStatus.PENDING.value)


class TradeEvent(Base):
    """Append-only audit rows. Nothing in the codebase updates or deletes these."""

    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Float)
    event_time: Mapped[datetime] = mapped_column(UtcDateTime)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"))
    payload_json: Mapped[dict | None] = mapped_column(JSON)


class ConfigSnapshot(Base):
    __tablename__ = "config_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    config_json: Mapped[dict] = mapped_column("json", JSON)
