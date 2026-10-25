from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ist() -> datetime:
    return now_utc().astimezone(IST)
