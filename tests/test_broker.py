import asyncio
import gzip
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.broker.base import BrokerAuthError, InstrumentNotFoundError
from app.broker.instruments import (
    InstrumentIndex,
    InstrumentMaster,
    parse_expiry_text,
    parse_option_instruments,
)
from app.broker.upstox import UpstoxBroker
from app.timeutil import IST

TODAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 10:30 IST


def expiry_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=IST).timestamp() * 1000)


def option_record(key: str, underlying: str, strike: float, kind: str, expiry: date) -> dict:
    return {
        "segment": "NSE_FO",
        "instrument_key": key,
        "trading_symbol": f"{underlying} {strike} {kind}",
        "underlying_symbol": underlying,
        "strike_price": strike,
        "instrument_type": kind,
        "expiry": expiry_ms(expiry),
        "lot_size": 30,
    }


RECORDS = [
    option_record("NSE_FO|1", "BANKNIFTY", 52000, "CE", date(2026, 11, 26)),
    option_record("NSE_FO|2", "BANKNIFTY", 52000, "CE", date(2026, 12, 31)),
    option_record("NSE_FO|3", "BANKNIFTY", 52000, "PE", date(2026, 11, 26)),
    option_record("NSE_FO|4", "NIFTY", 24500, "CE", date(2026, 11, 3)),
    option_record("NSE_FO|5", "NIFTY", 24500, "CE", date(2026, 10, 27)),  # already expired
    {"segment": "NSE_EQ", "instrument_key": "NSE_EQ|9", "instrument_type": "EQ"},
    {"segment": "NSE_FO", "instrument_key": "NSE_FO|8", "instrument_type": "FUT"},
]


def build_index() -> InstrumentIndex:
    return InstrumentIndex(parse_option_instruments(RECORDS))


def test_only_option_contracts_are_indexed():
    assert len(parse_option_instruments(RECORDS)) == 5


def test_nearest_expiry_is_chosen_when_none_is_given():
    found = build_index().find_option("BANKNIFTY", None, 52000, "CE", TODAY)
    assert found.instrument_key == "NSE_FO|1"


def test_explicit_expiry_is_respected():
    found = build_index().find_option("BANKNIFTY", date(2026, 12, 31), 52000, "CE", TODAY)
    assert found.instrument_key == "NSE_FO|2"


def test_expired_contracts_are_never_returned():
    found = build_index().find_option("NIFTY", None, 24500, "CE", TODAY)
    assert found.instrument_key == "NSE_FO|4"


def test_unknown_contract_raises():
    with pytest.raises(InstrumentNotFoundError):
        build_index().find_option("BANKNIFTY", None, 99999, "CE", TODAY)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-26", date(2026, 11, 26)),
        ("26 Nov 2026", date(2026, 11, 26)),
        ("26-NOV-26", date(2026, 11, 26)),
        ("26 NOV", date(2026, 11, 26)),
        ("05 JAN", date(2027, 1, 5)),  # already past this year, so next year
    ],
)
def test_expiry_text_is_parsed(text, expected):
    assert parse_expiry_text(text, TODAY) == expected


def test_unparseable_expiry_raises():
    with pytest.raises(InstrumentNotFoundError):
        parse_expiry_text("next week", TODAY)


class Downloads:
    def __init__(self) -> None:
        self.count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        return httpx.Response(200, content=gzip.compress(json.dumps(RECORDS).encode()))


async def test_instrument_master_downloads_once_per_ist_day(tmp_path: Path):
    downloads = Downloads()
    clock_value = [NOW]
    async with httpx.AsyncClient(transport=httpx.MockTransport(downloads.handler)) as client:
        master = InstrumentMaster(client, tmp_path, lambda: clock_value[0])
        await master.load_index()
        await master.load_index()
        assert downloads.count == 1

        # A fresh process the same day reuses the on-disk cache.
        await InstrumentMaster(client, tmp_path, lambda: clock_value[0]).load_index()
        assert downloads.count == 1

        clock_value[0] = NOW + timedelta(days=1)
        await master.load_index()
        assert downloads.count == 2


def build_broker(handler, tmp_path: Path, client: httpx.AsyncClient) -> UpstoxBroker:
    master = InstrumentMaster(client, tmp_path, lambda: NOW)
    return UpstoxBroker(client, "secret-token", master, 0.0, lambda: NOW)


async def test_ltp_is_read_and_token_is_sent_as_bearer(tmp_path: Path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["key"] = request.url.params["instrument_key"]
        body = {
            "status": "success",
            "data": {
                "NSE_FO:BANKNIFTY52000CE": {"last_price": 201.5, "instrument_token": "NSE_FO|1"}
            },
        }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        price = await build_broker(handler, tmp_path, client).get_ltp("NSE_FO|1")

    assert price == 201.5
    assert seen == {"auth": "Bearer secret-token", "key": "NSE_FO|1"}


async def test_rejected_token_fails_loudly_without_leaking_it(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BrokerAuthError) as error:
            await build_broker(handler, tmp_path, client).get_ltp("NSE_FO|1")

    assert "secret-token" not in str(error.value)


async def test_intraday_candles_are_parsed_sorted_and_clipped(tmp_path: Path):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        rows = [  # Upstox returns newest first
            ["2026-11-02T10:02:00+05:30", 3, 4, 2, 3.5, 100, 0],
            ["2026-11-02T10:01:00+05:30", 2, 3, 1, 2.5, 100, 0],
            ["2026-11-02T09:15:00+05:30", 1, 2, 0.5, 1.5, 100, 0],
        ]
        return httpx.Response(200, json={"status": "success", "data": {"candles": rows}})

    start = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        candles = await build_broker(handler, tmp_path, client).get_historical_candles(
            "NSE_FO|1", "1minute", start, start + timedelta(hours=1)
        )

    assert paths == ["/v2/historical-candle/intraday/NSE_FO%7C1/1minute"]
    assert [c.close for c in candles] == [2.5, 3.5]
    assert candles[0].time == datetime(2026, 11, 2, 4, 31, tzinfo=timezone.utc)


async def test_past_days_use_the_historical_endpoint(tmp_path: Path):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"status": "success", "data": {"candles": []}})

    start = datetime(2026, 10, 30, 4, 30, tzinfo=timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await build_broker(handler, tmp_path, client).get_historical_candles(
            "NSE_FO|1", "1minute", start, start + timedelta(hours=4)
        )

    assert paths == ["/v2/historical-candle/NSE_FO%7C1/1minute/2026-10-30/2026-10-30"]


async def test_stream_ltp_delivers_each_poll_until_cancelled(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"status": "success", "data": {"k": {"last_price": 100, "instrument_token": "NSE_FO|1"}}}
        return httpx.Response(200, json=body)

    received = []

    async def callback(key: str, price: float, time: datetime) -> None:
        received.append((key, price))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        broker = build_broker(handler, tmp_path, client)
        task = asyncio.create_task(broker.stream_ltp(["NSE_FO|1"], callback))
        for _ in range(100):
            if len(received) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    assert received[:3] == [("NSE_FO|1", 100.0)] * 3
