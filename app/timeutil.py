from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_ist(moment: datetime) -> datetime:
    return ensure_utc(moment).astimezone(IST)


def ensure_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc)
