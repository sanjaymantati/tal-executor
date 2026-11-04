import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import structlog

from app.broker.base import (
    BrokerAuthError,
    Candle,
    Instrument,
    InstrumentNotFoundError,
    LtpCallback,
)
from app.broker.instruments import InstrumentMaster, parse_expiry_text
from app.timeutil import IST

log = structlog.get_logger(__name__)

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UNAUTHORIZED_STATUS = 401


class UpstoxBroker:
    """Read-only Upstox v2 market-data client. Holds the daily access token."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        access_token: str,
        instrument_master: InstrumentMaster,
        poll_interval_sec: float,
        clock: Callable[[], datetime],
    ) -> None:
        self._http_client = http_client
        self._access_token = access_token
        self._instrument_master = instrument_master
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock

    async def resolve_instrument(
        self, underlying: str, expiry: str | None, strike: float, option_type: str
    ) -> Instrument:
        index = await self._instrument_master.load_index()
        today = self._instrument_master.today_ist()
        expiry_date = parse_expiry_text(expiry, today) if expiry else None
        return index.find_option(underlying, expiry_date, strike, option_type, today)

    async def get_ltp(self, instrument_key: str) -> float:
        prices = await self.get_ltps([instrument_key])
        if instrument_key not in prices:
            raise InstrumentNotFoundError(f"no LTP returned for {instrument_key}")
        return prices[instrument_key]

    async def get_ltps(self, instrument_keys: list[str]) -> dict[str, float]:
        """Keys the broker has no quote for are absent from the result."""
        return await self._fetch_ltps(instrument_keys)

    async def get_historical_candles(
        self, instrument_key: str, interval: str, start: datetime, end: datetime
    ) -> list[Candle]:
        today = self._clock().astimezone(IST).date()
        start_day = start.astimezone(IST).date()
        end_day = end.astimezone(IST).date()
        candles: list[Candle] = []
        if start_day < today:
            past_end = min(end_day, today - timedelta(days=1))
            candles += await self._fetch_past_candles(instrument_key, interval, start_day, past_end)
        if end_day >= today:
            candles += await self._fetch_intraday_candles(instrument_key, interval)
        in_range = [c for c in candles if start <= c.time <= end]
        return sorted(in_range, key=lambda c: c.time)

    async def stream_ltp(self, instrument_keys: list[str], callback: LtpCallback) -> None:
        """Polls REST LTP. The protobuf websocket feed can replace this behind the same method."""
        while True:
            await self._poll_once(instrument_keys, callback)
            await asyncio.sleep(self._poll_interval_sec)

    async def _poll_once(self, instrument_keys: list[str], callback: LtpCallback) -> None:
        try:
            prices = await self._fetch_ltps(instrument_keys)
        except httpx.HTTPError:
            log.warning("ltp_poll_failed")
            return
        received_at = self._clock()
        for key, price in prices.items():
            await callback(key, price, received_at)

    async def _fetch_ltps(self, instrument_keys: list[str]) -> dict[str, float]:
        payload = await self._get(
            "/market-quote/ltp", params={"instrument_key": ",".join(instrument_keys)}
        )
        return {
            entry["instrument_token"]: float(entry["last_price"])
            for entry in payload["data"].values()
        }

    async def _fetch_past_candles(
        self, instrument_key: str, interval: str, start_day: date, end_day: date
    ) -> list[Candle]:
        path = (
            f"/historical-candle/{quote(instrument_key, safe='')}/{interval}"
            f"/{end_day.isoformat()}/{start_day.isoformat()}"
        )
        return parse_candles((await self._get(path, params={}))["data"]["candles"])

    async def _fetch_intraday_candles(self, instrument_key: str, interval: str) -> list[Candle]:
        path = f"/historical-candle/intraday/{quote(instrument_key, safe='')}/{interval}"
        return parse_candles((await self._get(path, params={}))["data"]["candles"])

    async def _get(self, path: str, params: dict) -> dict:
        response = await self._http_client.get(
            f"{UPSTOX_BASE_URL}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
        )
        if response.status_code == UNAUTHORIZED_STATUS:
            raise BrokerAuthError("Upstox access token rejected or expired; set a fresh UPSTOX_ACCESS_TOKEN")
        response.raise_for_status()
        return response.json()


def parse_candles(rows: list[list]) -> list[Candle]:
    return [
        Candle(
            time=datetime.fromisoformat(row[0]).astimezone(timezone.utc),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
    ]
