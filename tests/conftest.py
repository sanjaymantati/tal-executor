from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from app.db.message_store import IncomingMessage
from app.db.models import Base
from app.db.session import create_engine, create_session_factory

CHANNEL_ID = -1001234567890
BASE_TIME = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)  # 10:00 IST


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def make_message(
    tg_message_id: int,
    text: str,
    minutes_after_base: float,
    reply_to_msg_id: int | None,
) -> IncomingMessage:
    return IncomingMessage(
        channel_id=CHANNEL_ID,
        tg_message_id=tg_message_id,
        reply_to_msg_id=reply_to_msg_id,
        text=text,
        sent_at=BASE_TIME + timedelta(minutes=minutes_after_base),
        edited_at=None,
        raw_json={"id": tg_message_id},
    )
