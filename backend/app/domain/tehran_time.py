from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")


def format_tehran(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN).strftime(fmt)


def tehran_now() -> datetime:
    return datetime.now(TEHRAN)


def tehran_midnight_to_utc(day: date) -> datetime:
    """Start of calendar day in Asia/Tehran, as UTC."""
    local = datetime.combine(day, time.min, tzinfo=TEHRAN)
    return local.astimezone(timezone.utc)


def tehran_end_of_day_to_utc(day: date) -> datetime:
    """End of calendar day in Asia/Tehran (23:59:59.999999), as UTC."""
    local = datetime.combine(day, time.max, tzinfo=TEHRAN)
    return local.astimezone(timezone.utc)


def report_period_bounds_utc(
    period: str,
    *,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Map report filters to UTC bounds using Asia/Tehran calendar where required."""
    now_tehran = tehran_now()
    now_utc = datetime.now(timezone.utc)
    if period == "today":
        start = tehran_midnight_to_utc(now_tehran.date())
        return start, now_utc
    if period == "7d":
        start_day = now_tehran.date() - timedelta(days=6)
        return tehran_midnight_to_utc(start_day), now_utc
    if period == "30d":
        start_day = now_tehran.date() - timedelta(days=29)
        return tehran_midnight_to_utc(start_day), now_utc
    if period == "custom" and custom_start and custom_end:
        start = tehran_midnight_to_utc(custom_start)
        end = min(tehran_end_of_day_to_utc(custom_end), now_utc)
        return start, end
    return None, None


def parse_tehran_date(value: str) -> date:
    """Parse YYYY-MM-DD as a Tehran calendar date (no timezone shift on the string)."""
    return date.fromisoformat(value[:10])


def tehran_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN).isoformat(timespec="seconds")
