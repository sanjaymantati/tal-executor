import json
from typing import Any

from app.db.message_store import IncomingMessage
from app.timeutil import ensure_utc


def to_incoming_message(message: Any, channel_id: int) -> IncomingMessage:
    return IncomingMessage(
        channel_id=channel_id,
        tg_message_id=message.id,
        reply_to_msg_id=message.reply_to_msg_id,
        text=getattr(message, "message", None) or "",
        sent_at=ensure_utc(message.date),
        edited_at=ensure_utc(message.edit_date) if message.edit_date else None,
        raw_json=serialise_raw(message),
    )


def serialise_raw(message: Any) -> dict | None:
    try:
        return json.loads(json.dumps(message.to_dict(), default=str))
    except Exception:
        return None
