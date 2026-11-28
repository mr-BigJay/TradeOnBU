"""Closed UTC weeks and calendar months for the four-hour analyst."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.services.daily_outlook_data import DAY, aggregate_complete, validate_bars


def period_starts(now_ms):
    midnight = datetime.fromtimestamp(now_ms / 1000, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday()), midnight.replace(day=1)


def monthly_rows(daily, month_start):
    by_time = {bar.time_ms: bar for bar in daily}
    months = []
    end = month_start
    for _ in range(12):
        start = (end - timedelta(days=1)).replace(day=1)
        rows = [by_time.get(t) for t in range(int(start.timestamp()*1000),
                                              int(end.timestamp()*1000), DAY)]
        if any(row is None for row in rows):
            raise ValueError("incomplete_calendar_month")
        months.append([rows[0].time_ms, rows[0].open, max(row.high for row in rows),
                       min(row.low for row in rows), rows[-1].close])
        end = start
    return months[::-1]


async def load_context(market, symbol, now_ms, previous):
    week, month = period_starts(now_ms)
    period = [week.date().isoformat(), month.date().isoformat()]
    cached = ((previous or {}).get("analyst_summary") or {}).get("long_term_context") or {}
    if cached.get("period") == period and cached.get("status") == "complete":
        return cached
    start = min(month.replace(year=month.year - 1), week - timedelta(weeks=52))
    try:
        bundle = await asyncio.wait_for(market.fetch_historical_closed(
            symbol, "4h", from_ms=int(start.timestamp()*1000), max_bars=2500), timeout=45)
        if not bundle.data_complete:
            raise ValueError("incomplete_history")
        daily = aggregate_complete(bundle.closed, days=1, now_ms=now_ms)
        # Only data ending before the current week/month can contribute.
        boundary = int(max(week, month).timestamp()*1000)
        daily = [bar for bar in daily if bar.time_ms < boundary]
        validate_bars(daily, step=DAY, now_ms=boundary, minimum=365)
        weeks = aggregate_complete(daily, days=7, now_ms=now_ms)[-52:]
        if len(weeks) != 52 or weeks[-1].time_ms + 7*DAY != int(week.timestamp()*1000):
            raise ValueError("incomplete_weeks")
        return {"status": "complete", "period": period, "columns": ["open_time_ms", "open",
                "high", "low", "close"], "1w": [[b.time_ms, b.open, b.high, b.low, b.close]
                for b in weeks], "1M": monthly_rows(daily, month)}
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "period": period, "reason": type(exc).__name__}
