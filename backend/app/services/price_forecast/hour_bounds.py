from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def next_six_hourly_closes(from_dt: datetime | None = None) -> list[datetime]:
    """End timestamps for the next six closed 1H candles (UTC hour boundaries)."""
    now = (from_dt or utc_now()).astimezone(timezone.utc)
    hour_floor = now.replace(minute=0, second=0, microsecond=0)
    first_close = hour_floor + timedelta(hours=1)
    return [first_close + timedelta(hours=i) for i in range(6)]


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
