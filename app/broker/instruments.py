import gzip
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import httpx
import structlog

from app.broker.base import Instrument, InstrumentNotFoundError
from app.timeutil import IST

log = structlog.get_logger(__name__)

INSTRUMENTS_URL_TEMPLATE = "https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.json.gz"
OPTION_SEGMENT_BY_EXCHANGE = {"NSE": "NSE_FO", "BSE": "BSE_FO"}
OPTION_SEGMENTS = frozenset(OPTION_SEGMENT_BY_EXCHANGE.values())
OPTION_TYPES = ("CE", "PE")
EXPIRY_TEXT_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d %b %y",
    "%d-%b-%y",
)
EXPIRY_TEXT_FORMATS_WITHOUT_YEAR = ("%d %b", "%d %B", "%d-%b", "%b %d", "%B %d")


class InstrumentIndex:
    """In-memory lookup of option contracts."""

    def __init__(self, instruments: list[Instrument]) -> None:
        self._instruments = instruments

    def find_option(
        self,
        underlying: str,
        expiry: date | None,
        strike: float,
        option_type: str,
        today: date,
    ) -> Instrument:
        candidates = [
            i
            for i in self._instruments
            if i.underlying == underlying
            and i.option_type == option_type
            and i.strike == strike
            and i.expiry >= today
        ]
        if expiry is not None:
            candidates = [i for i in candidates if i.expiry == expiry]
        if not candidates:
            raise InstrumentNotFoundError(
                f"no option for {underlying} {strike} {option_type} expiry={expiry}"
            )
        return min(candidates, key=lambda i: i.expiry)


class InstrumentMaster:
    """Downloads the Upstox instrument file once per IST day and caches it on disk."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        cache_dir: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self._http_client = http_client
        self._cache_dir = cache_dir
        self._clock = clock
        self._loaded_for: date | None = None
        self._index: InstrumentIndex | None = None

    async def load_index(self) -> InstrumentIndex:
        today = self.today_ist()
        if self._index is None or self._loaded_for != today:
            records = await self._read_records(today)
            self._index = InstrumentIndex(parse_option_instruments(records))
            self._loaded_for = today
        return self._index

    def today_ist(self) -> date:
        return self._clock().astimezone(IST).date()

    async def _read_records(self, today: date) -> list[dict]:
        records: list[dict] = []
        for exchange in OPTION_SEGMENT_BY_EXCHANGE:
            records += await self._read_exchange_records(exchange, today)
        return records

    async def _read_exchange_records(self, exchange: str, today: date) -> list[dict]:
        cache_file = self._cache_dir / f"{exchange}_{today.isoformat()}.json.gz"
        if not cache_file.exists():
            await self._download(exchange, cache_file)
        with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
            return json.load(handle)

    async def _download(self, exchange: str, cache_file: Path) -> None:
        url = INSTRUMENTS_URL_TEMPLATE.format(exchange=exchange)
        log.info("instrument_master_download", url=url)
        response = await self._http_client.get(url, follow_redirects=True)
        response.raise_for_status()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(response.content)


def parse_option_instruments(records: list[dict]) -> list[Instrument]:
    return [
        build_instrument(record)
        for record in records
        if record.get("segment") in OPTION_SEGMENTS
        and record.get("instrument_type") in OPTION_TYPES
    ]


def build_instrument(record: dict) -> Instrument:
    return Instrument(
        instrument_key=record["instrument_key"],
        trading_symbol=record["trading_symbol"],
        underlying=str(record["underlying_symbol"]).upper(),
        expiry=datetime.fromtimestamp(record["expiry"] / 1000, tz=IST).date(),
        strike=float(record["strike_price"]),
        option_type=record["instrument_type"],
        lot_size=int(record.get("lot_size") or 0),
    )


def parse_expiry_text(text: str, today: date) -> date:
    cleaned = text.strip().upper().replace(",", " ")
    for pattern in EXPIRY_TEXT_FORMATS:
        parsed = try_parse_date(cleaned, pattern)
        if parsed is not None:
            return parsed
    for pattern in EXPIRY_TEXT_FORMATS_WITHOUT_YEAR:
        parsed = try_parse_date(cleaned, pattern)
        if parsed is not None:
            return nearest_upcoming(parsed.month, parsed.day, today)
    raise InstrumentNotFoundError(f"cannot understand expiry text: {text!r}")


def try_parse_date(text: str, pattern: str) -> date | None:
    try:
        return datetime.strptime(text, pattern).date()
    except ValueError:
        return None


def nearest_upcoming(month: int, day: int, today: date) -> date:
    this_year = date(today.year, month, day)
    return this_year if this_year >= today else date(today.year + 1, month, day)
