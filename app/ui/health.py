import asyncio
import base64
import json
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from telethon import TelegramClient, utils

from app.status import PipelineStatusStore

# Market data is all the app needs, and it is what read-only (analytics) tokens allow;
# the profile endpoint rejects those tokens, so it would be a misleading health signal.
UPSTOX_PROBE_URL = "https://api.upstox.com/v2/market-quote/ltp"
UPSTOX_PROBE_INSTRUMENT = "NSE_INDEX|Nifty 50"
TELEGRAM_CHECK_TIMEOUT_SEC = 20.0
UPSTOX_CHECK_TIMEOUT_SEC = 10.0


class HealthCheck(Protocol):
    async def check(self) -> dict[str, Any]: ...


class UpstoxHealthCheck:
    """Calls Upstox with the configured access token. Never returns the token itself."""

    def __init__(
        self, http_client: httpx.AsyncClient, access_token: str, clock: Callable[[], datetime]
    ) -> None:
        self._http_client = http_client
        self._access_token = access_token
        self._clock = clock

    async def check(self) -> dict[str, Any]:
        result = {"checked_at": self._clock(), **describe_token_expiry(self._access_token, self._clock())}
        if not self._access_token:
            return {**result, "ok": False, "message": "UPSTOX_ACCESS_TOKEN is empty"}
        try:
            response = await self._http_client.get(
                UPSTOX_PROBE_URL,
                params={"instrument_key": UPSTOX_PROBE_INSTRUMENT},
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
                timeout=UPSTOX_CHECK_TIMEOUT_SEC,
            )
        except httpx.HTTPError as error:
            return {**result, "ok": False, "message": f"Cannot reach Upstox: {type(error).__name__}"}
        return {**result, **interpret_probe_response(response)}


class ListenerAwareTelegramHealth:
    """Reports the running listener's own state instead of probing Telegram.

    Telegram delivers live updates to the newest connection of a login, so a probe opened while the
    pipeline listens silently steals its messages. A probe is only safe when nothing is listening.
    """

    def __init__(
        self, status_store: PipelineStatusStore, probe: HealthCheck, clock: Callable[[], datetime]
    ) -> None:
        self._status_store = status_store
        self._probe = probe
        self._clock = clock

    async def check(self) -> dict[str, Any]:
        if not self._status_store.is_pipeline_running():
            return await self._probe.check()
        return self._describe_listener(self._status_store.read().get("telegram", {}))

    def _describe_listener(self, telegram: dict[str, Any]) -> dict[str, Any]:
        state = telegram.get("state")
        return {
            "checked_at": self._clock(),
            "ok": state == "listening",
            "authorized": True,
            "session_file_exists": True,
            "message": f"Pipeline is running, Telegram {state or 'state unknown'}",
            "channel_title": telegram.get("channel_title"),
            "channel_id": telegram.get("channel_id"),
        }


class TelegramHealthCheck:
    """Probes the saved Telegram session using a throw-away copy of the session file.

    The copy keeps the live pipeline's session file untouched, but the probe is a second connection
    with the same login, which steals live updates from a running listener. Only run it when no
    pipeline is listening (see ListenerAwareTelegramHealth).
    """

    def __init__(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        channel_reference: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_file = resolve_session_file(session_name)
        self._api_id = api_id
        self._api_hash = api_hash
        self._channel_reference = channel_reference
        self._clock = clock

    async def check(self) -> dict[str, Any]:
        base = {
            "checked_at": self._clock(),
            "session_file": self._session_file.name,
            "session_file_exists": self._session_file.exists(),
        }
        if not self._session_file.exists():
            message = "No session file yet. Log in once with `python get_chat_id.py`."
            return {**base, "ok": False, "authorized": False, "message": message}
        try:
            details = await asyncio.wait_for(self._probe_session(), TELEGRAM_CHECK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return {**base, "ok": False, "authorized": False, "message": "Telegram timed out"}
        except Exception as error:
            message = f"Telegram check failed: {type(error).__name__}"
            return {**base, "ok": False, "authorized": False, "message": message}
        return {**base, **details}

    async def _probe_session(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            probe_path = Path(directory) / "probe.session"
            shutil.copy(self._session_file, probe_path)
            client = TelegramClient(str(probe_path.with_suffix("")), self._api_id, self._api_hash)
            await client.connect()
            try:
                return await self._describe_session(client)
            finally:
                await client.disconnect()

    async def _describe_session(self, client: TelegramClient) -> dict[str, Any]:
        if not await client.is_user_authorized():
            message = "Session exists but is not logged in. Log in again."
            return {"ok": False, "authorized": False, "message": message}
        me = await client.get_me()
        details: dict[str, Any] = {
            "ok": True,
            "authorized": True,
            "account": utils.get_display_name(me),
            "message": "Telegram session is logged in",
        }
        details.update(await self._describe_channel(client))
        return details

    async def _describe_channel(self, client: TelegramClient) -> dict[str, Any]:
        try:
            entity = await client.get_entity(parse_reference(self._channel_reference))
        except Exception:
            return {"channel_message": f"Cannot resolve TG_CHANNEL ({self._channel_reference})"}
        return {
            "channel_title": utils.get_display_name(entity),
            "channel_id": utils.get_peer_id(entity),
        }


def resolve_session_file(session_name: str) -> Path:
    name = session_name if session_name.endswith(".session") else f"{session_name}.session"
    return Path(name)


def parse_reference(reference: str) -> int | str:
    try:
        return int(reference)
    except ValueError:
        return reference


def describe_token_expiry(access_token: str, now: datetime) -> dict[str, Any]:
    """Upstox tokens are JWTs; reading `exp` needs no signature check."""
    expires_at = read_jwt_expiry(access_token)
    if expires_at is None:
        return {"token_expires_at": None, "token_minutes_left": None}
    minutes_left = int((expires_at - now).total_seconds() // 60)
    return {"token_expires_at": expires_at, "token_minutes_left": minutes_left}


def read_jwt_expiry(token: str) -> datetime | None:
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return datetime.fromtimestamp(json.loads(decoded)["exp"], tz=timezone.utc)
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def interpret_probe_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 401:
        return {"ok": False, "message": "Access token rejected or expired. Set a fresh token."}
    if response.status_code != 200:
        return {"ok": False, "message": f"Upstox returned HTTP {response.status_code}"}
    quotes = list(response.json().get("data", {}).values())
    nifty_ltp = quotes[0].get("last_price") if quotes else None
    return {"ok": True, "message": "Upstox token works for market data", "nifty_ltp": nifty_ltp}
