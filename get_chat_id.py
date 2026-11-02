import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

async def main():
    async with TelegramClient(
        "telegram_session",
        int(os.getenv("TG_API_ID")),
        os.getenv("TG_API_HASH")
    ) as client:

        async for dialog in client.iter_dialogs():
            print(f"Group: {dialog.name}")
            print(f"Chat ID: {dialog.id}")
            print("-" * 40)

asyncio.run(main())