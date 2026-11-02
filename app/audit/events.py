from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TradeEvent
from app.enums import EventType


def record_trade_event(
    session: AsyncSession,
    trade_id: int,
    event_type: EventType,
    event_time: datetime,
    price: float | None,
    source_message_id: int | None,
    payload: dict | None,
) -> TradeEvent:
    """Append an immutable audit row. Events are only ever inserted, never changed."""
    event = TradeEvent(
        trade_id=trade_id,
        event_type=event_type.value,
        price=price,
        event_time=event_time,
        source_message_id=source_message_id,
        payload_json=payload,
    )
    session.add(event)
    return event
