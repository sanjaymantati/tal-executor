from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.db.models import (
    MessageClassification,
    PaperTrade,
    RawMessage,
    Trade,
    TradeEvent,
    TradeFieldHistory,
    TradeMessage,
)
from app.db.session import SessionFactory
from app.enums import ClassificationStatus, EventType
from app.execution.types import TradingCosts
from app.report import ReportBuilder
from app.ui.premiums import (
    ACTIVE_PAPER_STATUSES,
    LivePriceQuotes,
    describe_live_values,
    is_active_paper_trade,
    is_from_earlier_session,
)

MAX_LIMIT = 500
FINISHED_STATUSES = (ClassificationStatus.OK.value, ClassificationStatus.FAILED.value)


class DashboardQueries:
    """Read-only views of the database for the dashboard."""

    def __init__(
        self,
        session_factory: SessionFactory,
        quotes: LivePriceQuotes,
        costs: TradingCosts,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._quotes = quotes
        self._costs = costs
        self._clock = clock

    async def summarise(self) -> dict[str, Any]:
        report = await ReportBuilder(self._session_factory).build_report()
        async with self._session_factory() as session:
            totals = await session.execute(
                select(
                    func.count(RawMessage.id),
                    func.max(RawMessage.sent_at),
                    func.max(RawMessage.created_at),
                )
            )
            total_messages, last_sent_at, last_captured_at = totals.one()
            pending = await self._count_unparsed_messages(session)
        return {
            "messages_captured": total_messages,
            "messages_waiting_for_parsing": pending,
            "last_message_sent_at": last_sent_at,
            "last_message_captured_at": last_captured_at,
            "trades_captured": report.trades_captured,
            "trades_by_state": report.trades_by_state,
            "incomplete_trades": report.incomplete_trades,
            "paper_trades_closed": report.closed_paper_trades,
            "paper_trades_cancelled": report.cancelled_paper_trades,
            "win_rate": report.win_rate,
            "total_pnl": report.total_pnl,
            "average_r_multiple": report.average_r_multiple,
        }

    async def list_recent_messages(self, limit: int) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(RawMessage)
                .order_by(RawMessage.tg_message_id.desc(), RawMessage.id.desc())
                .limit(clamp_limit(limit))
            )
            messages = list(result.scalars())
            ids = [message.id for message in messages]
            classifications = await self._load_latest_classifications(session, ids)
            links = await self._load_trade_links(session, ids)
        return [
            describe_message(m, classifications.get(m.id), links.get(m.id)) for m in messages
        ]

    async def list_trades(self, limit: int) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trade).order_by(Trade.id.desc()).limit(clamp_limit(limit))
            )
            trades = list(result.scalars())
            ids = [trade.id for trade in trades]
            papers = await self._load_paper_trades(session, ids)
            message_counts = await self._count_trade_messages(session, ids)
            stop_requested = await self._load_stop_requested_trade_ids(session, ids)
        live_values = await self._find_live_values(trades, papers)
        return [
            {
                **describe_trade(trade),
                "message_count": message_counts.get(trade.id, 0),
                "paper_trade": describe_paper_trade(
                    papers.get(trade.id), trade.id in stop_requested, live_values.get(trade.id)
                ),
            }
            for trade in trades
        ]

    async def describe_trade_in_detail(self, trade_id: int) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            trade = await session.get(Trade, trade_id)
            if trade is None:
                return None
            paper = await self._load_paper_trades(session, [trade_id])
            messages = await self._load_trade_messages(session, trade_id)
            history = await self._load_field_history(session, trade_id)
            events = await self._load_trade_events(session, trade_id)
            stop_requested = await self._load_stop_requested_trade_ids(session, [trade_id])
        live_values = await self._find_live_values([trade], paper)
        return {
            **describe_trade(trade),
            "paper_trade": describe_paper_trade(
                paper.get(trade_id), trade_id in stop_requested, live_values.get(trade_id)
            ),
            "messages": messages,
            "field_history": history,
            "events": events,
        }

    async def list_recent_events(self, limit: int) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TradeEvent, Trade)
                .join(Trade, Trade.id == TradeEvent.trade_id)
                .order_by(TradeEvent.id.desc())
                .limit(clamp_limit(limit))
            )
            return [
                {**describe_event(event), "trade": label_trade(trade)}
                for event, trade in result.all()
            ]

    async def _find_live_values(
        self, trades: list[Trade], papers: dict[int, PaperTrade]
    ) -> dict[int, dict[str, Any]]:
        keys = find_live_instrument_keys(trades, papers, self._clock())
        premiums = await self._quotes.find_premiums(keys)
        return {
            trade.id: describe_live_values(
                papers[trade.id], premiums, trade.instrument_key, self._costs
            )
            for trade in trades
            if trade.id in papers
        }

    async def _count_unparsed_messages(self, session) -> int:
        finished = select(MessageClassification.id).where(
            MessageClassification.message_id == RawMessage.id,
            MessageClassification.status.in_(FINISHED_STATUSES),
        )
        result = await session.execute(
            select(func.count(RawMessage.id)).where(~finished.exists())
        )
        return result.scalar_one()

    async def _load_latest_classifications(
        self, session, message_ids: list[int]
    ) -> dict[int, MessageClassification]:
        result = await session.execute(
            select(MessageClassification)
            .where(
                MessageClassification.message_id.in_(message_ids),
                MessageClassification.status != ClassificationStatus.STALE.value,
            )
            .order_by(MessageClassification.id)
        )
        return {row.message_id: row for row in result.scalars()}

    async def _load_trade_links(self, session, message_ids: list[int]) -> dict[int, TradeMessage]:
        result = await session.execute(
            select(TradeMessage).where(TradeMessage.message_id.in_(message_ids))
        )
        return {row.message_id: row for row in result.scalars()}

    async def _load_paper_trades(self, session, trade_ids: list[int]) -> dict[int, PaperTrade]:
        result = await session.execute(
            select(PaperTrade).where(PaperTrade.trade_id.in_(trade_ids))
        )
        return {row.trade_id: row for row in result.scalars()}

    async def _load_stop_requested_trade_ids(self, session, trade_ids: list[int]) -> set[int]:
        result = await session.execute(
            select(TradeEvent.trade_id)
            .where(
                TradeEvent.trade_id.in_(trade_ids),
                TradeEvent.event_type == EventType.MANUAL_STOP_REQUESTED.value,
            )
            .distinct()
        )
        return set(result.scalars())

    async def _count_trade_messages(self, session, trade_ids: list[int]) -> dict[int, int]:
        result = await session.execute(
            select(TradeMessage.trade_id, func.count(TradeMessage.message_id))
            .where(TradeMessage.trade_id.in_(trade_ids))
            .group_by(TradeMessage.trade_id)
        )
        return {trade_id: count for trade_id, count in result.all()}

    async def _load_trade_messages(self, session, trade_id: int) -> list[dict[str, Any]]:
        result = await session.execute(
            select(RawMessage, TradeMessage.role)
            .join(TradeMessage, TradeMessage.message_id == RawMessage.id)
            .where(TradeMessage.trade_id == trade_id)
            .order_by(RawMessage.sent_at, RawMessage.id)
        )
        return [
            {
                "tg_message_id": message.tg_message_id,
                "sent_at": message.sent_at,
                "text": message.text,
                "role": role,
            }
            for message, role in result.all()
        ]

    async def _load_field_history(self, session, trade_id: int) -> list[dict[str, Any]]:
        result = await session.execute(
            select(TradeFieldHistory)
            .where(TradeFieldHistory.trade_id == trade_id)
            .order_by(TradeFieldHistory.id)
        )
        return [
            {
                "field": row.field,
                "old_value": row.old_value,
                "new_value": row.new_value,
                "changed_at": row.changed_at,
            }
            for row in result.scalars()
        ]

    async def _load_trade_events(self, session, trade_id: int) -> list[dict[str, Any]]:
        result = await session.execute(
            select(TradeEvent).where(TradeEvent.trade_id == trade_id).order_by(TradeEvent.id)
        )
        return [describe_event(event) for event in result.scalars()]


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def find_live_instrument_keys(
    trades: list[Trade], papers: dict[int, PaperTrade], now: datetime
) -> list[str]:
    """A trade left active from an earlier day has no live market, so it is not worth a request."""
    keys = {
        trade.instrument_key
        for trade in trades
        if trade.instrument_key
        and trade.id in papers
        and is_active_paper_trade(papers[trade.id])
        and not is_from_earlier_session(trade.created_at, now)
    }
    return sorted(keys)


def describe_message(
    message: RawMessage,
    classification: MessageClassification | None,
    link: TradeMessage | None,
) -> dict[str, Any]:
    return {
        "tg_message_id": message.tg_message_id,
        "reply_to_msg_id": message.reply_to_msg_id,
        "sent_at": message.sent_at,
        "edited_at": message.edited_at,
        "text": message.text,
        "parsed": describe_classification(classification),
        "trade_id": link.trade_id if link else None,
        "trade_role": link.role if link else None,
    }


def describe_classification(classification: MessageClassification | None) -> dict[str, Any] | None:
    if classification is None:
        return None
    fields = classification.fields_json or {}
    return {
        "status": classification.status,
        "message_type": classification.message_type,
        "confidence": classification.confidence,
        "fields": {name: value for name, value in fields.items() if value not in (None, [])},
        "references_msg_id": classification.references_msg_id,
        "model": classification.model,
        "latency_ms": classification.latency_ms,
        "classified_at": classification.created_at,
    }


def label_trade(trade: Trade) -> str:
    parts = [trade.underlying, f"{trade.strike:g}" if trade.strike else None, trade.option_type]
    label = " ".join(part for part in parts if part)
    return f"#{trade.id} {label}".strip()


def describe_trade(trade: Trade) -> dict[str, Any]:
    return {
        "id": trade.id,
        "label": label_trade(trade),
        "underlying": trade.underlying,
        "expiry": trade.expiry,
        "strike": trade.strike,
        "option_type": trade.option_type,
        "instrument_key": trade.instrument_key,
        "entry_low": trade.entry_low,
        "entry_high": trade.entry_high,
        "stop_loss": trade.stop_loss,
        "targets": trade.targets_json or [],
        "state": trade.state,
        "created_at": trade.created_at,
        "completed_at": trade.completed_at,
    }


def describe_paper_trade(
    paper: PaperTrade | None, stop_requested: bool, live_values: dict[str, Any] | None
) -> dict[str, Any] | None:
    if paper is None:
        return None
    return {
        **live_values,
        "status": paper.status,
        "stoppable": paper.status in ACTIVE_PAPER_STATUSES,
        "stop_requested": stop_requested and paper.status in ACTIVE_PAPER_STATUSES,
        "qty": paper.qty,
        "entry_price": paper.entry_price,
        "entry_time": paper.entry_time,
        "exit_price": paper.exit_price,
        "exit_time": paper.exit_time,
        "exit_reason": paper.exit_reason,
        "pnl": paper.pnl,
        "r_multiple": paper.r_multiple,
        "fees": paper.fees,
    }


def describe_event(event: TradeEvent) -> dict[str, Any]:
    event_time: datetime = event.event_time
    return {
        "id": event.id,
        "trade_id": event.trade_id,
        "event_type": event.event_type,
        "price": event.price,
        "event_time": event_time,
        "source_message_id": event.source_message_id,
        "payload": event.payload_json,
    }
