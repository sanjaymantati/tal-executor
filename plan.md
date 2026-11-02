# Plan: Live Telegram Group Message Ingestion

## Goal

Receive new messages in a Telegram group where I am a regular member (not the owner), and persist them to PostgreSQL in near real time using my own Telegram account.

## Approach

- Use **Telethon** (Telegram client API), authenticated as my Telegram user account.
- Use **PostgreSQL** for persistence.
- Run a long-lived Python process that listens for new messages.
- Filter events to the target group once its chat ID is known.

> This approach uses my user account rather than a BotFather bot. Only collect messages I am authorized to access, and follow Telegram's terms and the group's privacy expectations.

## Prerequisites

- Python 3.10+ recommended.
- PostgreSQL database available locally or remotely.
- Telegram API credentials from https://my.telegram.org:
  - `api_id`
  - `api_hash`
- Phone number associated with my Telegram account.
- Access to the target group through my Telegram account.

## Implementation Steps

### 1. Create the project

```bash
mkdir telegram-live
cd telegram-live
python -m venv .venv

# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\\Scripts\\Activate.ps1

pip install telethon asyncpg python-dotenv
```

Create these files:

```text
telegram-live/
├── main.py
├── .env
├── .gitignore
└── requirements.txt
```

### 2. Add dependencies

`requirements.txt`:

```text
telethon
asyncpg
python-dotenv
```

Install them with:

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

`.env`:

```dotenv
TG_API_ID=123456
TG_API_HASH=replace_with_your_api_hash
TG_PHONE=+91XXXXXXXXXX
DATABASE_URL=postgresql://postgres:replace_me@localhost:5432/telegram_db
TARGET_CHAT_ID=
```

Replace all example values. Do not commit `.env`, API credentials, or the Telethon session file to source control.

`.gitignore`:

```gitignore
.env
*.session
*.session-journal
.venv/
__pycache__/
```

Create the PostgreSQL database if it does not exist:

```sql
CREATE DATABASE telegram_db;
```

### 4. Implement the listener

`main.py`:

```python
import asyncio
import os
import logging

import asyncpg
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Leave TARGET_CHAT_ID empty during initial discovery.
target_chat_id_raw = os.getenv("TARGET_CHAT_ID", "").strip()
TARGET_CHAT_ID = int(target_chat_id_raw) if target_chat_id_raw else None

client = TelegramClient("telegram_session", API_ID, API_HASH)


async def main():
    db = await asyncpg.connect(DATABASE_URL)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS group_messages (
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            sender_id BIGINT,
            message_text TEXT,
            message_date TIMESTAMPTZ,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, message_id)
        )
    """)

    @client.on(events.NewMessage)
    async def on_new_message(event):
        if not event.is_group:
            return

        if TARGET_CHAT_ID is not None and event.chat_id != TARGET_CHAT_ID:
            return

        message = event.message
        text = message.message or ""

        try:
            await db.execute(
                """
                INSERT INTO group_messages (
                    chat_id, message_id, sender_id,
                    message_text, message_date
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, message_id) DO NOTHING
                """,
                event.chat_id,
                message.id,
                message.sender_id,
                text,
                message.date,
            )
            logger.info(
                "Stored group message: chat_id=%s message_id=%s",
                event.chat_id,
                message.id,
            )
        except Exception:
            logger.exception("Failed to store message")

    try:
        await client.start(phone=PHONE)
        logger.info("Listening for new group messages")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
```

### 5. Authenticate and run

```bash
python main.py
```

On first run, Telethon will ask for the login code sent by Telegram and may ask for the two-step verification password. This creates `telegram_session.session`, which allows later runs to reuse the login.

Keep the session file private. Anyone who obtains it may be able to access the Telegram account.

### 6. Identify the target group

Initially, leave `TARGET_CHAT_ID` blank. While the listener runs, add temporary logging inside the handler:

```python
chat = await event.get_chat()
logger.info("Group: %s | chat_id: %s", getattr(chat, "title", None), event.chat_id)
```

Send a test message in the intended group and note its ID. Stop the process, set `TARGET_CHAT_ID` in `.env`, and restart it. Remove or reduce diagnostic logging after setup.

### 7. Verify database writes

```sql
SELECT chat_id, message_id, sender_id, message_text, message_date
FROM group_messages
ORDER BY received_at DESC
LIMIT 20;
```

## Validation Checklist

- [ ] Telegram API credentials are valid.
- [ ] PostgreSQL is reachable.
- [ ] First-run login completes successfully.
- [ ] A new message in the target group is logged and saved.
- [ ] Messages from unrelated groups are ignored after configuring `TARGET_CHAT_ID`.
- [ ] Duplicate message inserts do not create duplicate rows.
- [ ] The process reconnects or is restarted after network/process failures.
- [ ] Secrets and session files are excluded from Git.

## Production Hardening

1. Run the listener under a process supervisor (systemd, Docker restart policy, or similar).
2. Add structured logs, health checks, and alerts for database or Telegram connection failures.
3. Use bounded retries with backoff for transient database errors.
4. Consider a connection pool (`asyncpg.create_pool`) for higher throughput.
5. Add fields for media metadata if photos, documents, or videos must be tracked; downloading media should be a separate, explicit requirement.
6. Define a retention policy and restrict database access.
7. Do not assume this listener backfills old messages. Live event handlers primarily process updates received while connected; implement a separate, authorized history-sync process if needed.
8. Review Telegram's terms and the group's rules before storing or processing other members' messages.

## Scope and Limitations

- This is a live listener for new group messages visible to the authenticated account.
- It does not require group ownership or adding a bot.
- It does not automatically download media files.
- It does not automatically import historical messages.
- Telegram connectivity, permissions, rate limits, and account security affect reliability.
