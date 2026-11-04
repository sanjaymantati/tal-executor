"""Dump the configured Telegram channel's messages from the last N days to a JSON file.

Usage (from the repo root, with the app stopped so the session file is not locked):
    python -m scripts.fetch_channel_messages --days 20 --output data/channel_messages.json
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom import Message

from app.config import Settings

BARE_CHANNEL_ID_DIGITS = 13
CHANNEL_ID_PREFIX = "100"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch recent Telegram channel messages")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("data/channel_messages.json"))
    return parser.parse_args()


def resolve_channel_reference(reference: str) -> int | str:
    """Telethon needs channel ids as -100<id>; .env often holds them without the minus sign."""
    try:
        numeric = int(reference)
    except ValueError:
        return reference
    is_bare_channel_id = str(numeric).startswith(CHANNEL_ID_PREFIX) and (
        len(str(numeric)) == BARE_CHANNEL_ID_DIGITS
    )
    return -numeric if is_bare_channel_id else numeric


def compute_cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def fetch_messages_since(
    client: TelegramClient, channel: int | str, cutoff: datetime
) -> list[dict[str, Any]]:
    entity = await client.get_entity(channel)
    messages: list[dict[str, Any]] = []
    # iter_messages walks newest -> oldest, so we can stop at the first message past the cutoff
    async for message in client.iter_messages(entity):
        if message.date < cutoff:
            break
        messages.append(serialize_message(message))
    return messages


def serialize_message(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "date": message.date.isoformat(),
        "edit_date": message.edit_date.isoformat() if message.edit_date else None,
        "reply_to_msg_id": message.reply_to_msg_id,
        "text": message.message or "",
        "has_media": message.media is not None,
        "views": message.views,
    }


def write_messages(messages: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    chronological = sorted(messages, key=lambda item: item["id"])
    output.write_text(json.dumps(chronological, indent=2, ensure_ascii=False), encoding="utf-8")


async def run(days: int, output: Path) -> None:
    settings = Settings()
    channel = resolve_channel_reference(settings.tg_channel)
    async with TelegramClient(
        settings.tg_session_name, settings.tg_api_id, settings.tg_api_hash
    ) as client:
        messages = await fetch_messages_since(client, channel, compute_cutoff(days))
    write_messages(messages, output)
    print(f"Saved {len(messages)} messages from the last {days} days to {output}")


def main() -> None:
    args = parse_args()
    asyncio.run(run(args.days, args.output))


if __name__ == "__main__":
    main()
