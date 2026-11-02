import asyncio
import logging
import os
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", "data/messages.db")).resolve()

target_chat_id_raw = os.getenv("TARGET_CHAT_ID", "").strip()
TARGET_CHAT_ID = int(target_chat_id_raw) if target_chat_id_raw else None

client = TelegramClient("telegram_session", API_ID, API_HASH)


async def main():
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DATABASE_FILE)

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sender_id INTEGER,
            message_text TEXT,
            message_date TEXT,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    await db.commit()

    @client.on(events.NewMessage)
    async def on_new_message(event):
        if not event.is_group:
            return

        if TARGET_CHAT_ID is not None and event.chat_id != TARGET_CHAT_ID:
            return

        message = event.message
        text = message.message or ""
        message_date = message.date.isoformat() if message.date else None

        try:
            await db.execute(
                """
                INSERT INTO group_messages (
                    chat_id, message_id, sender_id,
                    message_text, message_date
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (chat_id, message_id) DO NOTHING
                """,
                (
                    event.chat_id,
                    message.id,
                    message.sender_id,
                    text,
                    message_date,
                ),
            )
            await db.commit()
            logger.info(
                "Stored group message: chat_id=%s message_id=%s",
                event.chat_id,
                message.id,
            )
        except Exception:
            logger.exception("Failed to store message")

    try:
        await client.start(phone=PHONE)
        logger.info("Listening for new group messages (db file: %s)", DATABASE_FILE)
        await client.run_until_disconnected()
    finally:
        await db.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
