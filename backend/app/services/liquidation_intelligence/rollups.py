from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.liquidation_intelligence.analytics import WINDOW_MS
from app.services.liquidation_intelligence.repository import (
    aggregate_liquidation_buckets,
    get_rollup_cursor,
    upsert_window_rollup,
)


def bucket_start_for_timestamp(timestamp_ms: int, window_ms: int) -> int:
    return (int(timestamp_ms) // window_ms) * window_ms


def _empty_closed_payload(*, bucket_start: int, bucket_end: int) -> dict[str, Any]:
    return {
        "bucket_start_ms": bucket_start,
        "bucket_end_ms": bucket_end,
        "long_liquidation_notional_usd": 0.0,
        "short_liquidation_notional_usd": 0.0,
        "total_observed_notional_usd": 0.0,
        "long_short_ratio": None,
        "event_count": 0,
        "largest_observed_usd": 0.0,
        "time_bucket_closed": True,
        "connection_coverage_complete": None,
        "observation": "no_events_observed",
        "rollup_kind": "time_aligned_event_sum",
        "note": "Closed bucket with no raw events stored — not proof of zero market liquidations",
    }


def _missing_closed_buckets_between(
    populated_starts: set[int],
    *,
    window_ms: int,
    now_ms: int,
    span_min: int,
    span_max: int,
) -> list[int]:
    """Closed buckets with no events strictly between span_min and span_max (bounded)."""
    if span_max <= span_min:
        return []
    missing: list[int] = []
    b = span_min
    while b <= span_max:
        if b not in populated_starts and now_ms >= b + window_ms:
            missing.append(b)
        b += window_ms
    return missing


async def materialize_rollups_for_symbol(
    session: AsyncSession,
    *,
    symbol: str = "BTCUSDT",
    now_ms: int,
    sources: tuple[str, ...] = ("bybit", "binance", "combined"),
) -> int:
    """Incremental SQL rollups + bounded closed empty buckets between populated spans."""
    settings = get_settings()
    raw_cutoff_ms = now_ms - int(settings.liquidation_raw_retention_days) * 86400 * 1000
    written = 0
    until_ms = now_ms + 1
    for window_name, window_ms in WINDOW_MS.items():
        for source in sources:
            cursor = await get_rollup_cursor(session, symbol=symbol, window_name=window_name, source_exchange=source)
            overlap = window_ms if cursor.last_closed_bucket_start_ms > 0 else 0
            since_ms = max(raw_cutoff_ms, int(cursor.last_closed_bucket_start_ms) - overlap)
            aggregates = await aggregate_liquidation_buckets(
                session,
                symbol=symbol,
                window_ms=window_ms,
                since_ms=since_ms,
                until_ms=until_ms,
                source_exchange=source,
            )
            populated_starts = {int(a["bucket_start_ms"]) for a in aggregates}
            written_starts: set[int] = set()
            span_min = min(populated_starts) if populated_starts else 0
            span_max = max(populated_starts) if populated_starts else 0
            max_closed = int(cursor.last_closed_bucket_start_ms or 0)
            for agg in aggregates:
                bucket_start = int(agg["bucket_start_ms"])
                bucket_end = bucket_start + window_ms
                time_bucket_closed = now_ms >= bucket_end
                payload: dict[str, Any] = {
                    **agg,
                    "bucket_end_ms": bucket_end,
                    "time_bucket_closed": time_bucket_closed,
                    "connection_coverage_complete": None,
                    "rollup_kind": "time_aligned_event_sum",
                    "observation": "complete" if time_bucket_closed else "in_progress",
                }
                await upsert_window_rollup(
                    session,
                    symbol=symbol,
                    source_exchange=source,
                    window_name=window_name,
                    bucket_start_ms=bucket_start,
                    payload=payload,
                )
                written += 1
                written_starts.add(bucket_start)
                if time_bucket_closed and bucket_start > max_closed:
                    max_closed = bucket_start
            if populated_starts:
                for bucket_start in _missing_closed_buckets_between(
                    populated_starts,
                    window_ms=window_ms,
                    now_ms=now_ms,
                    span_min=span_min,
                    span_max=span_max,
                ):
                    bucket_end = bucket_start + window_ms
                    await upsert_window_rollup(
                        session,
                        symbol=symbol,
                        source_exchange=source,
                        window_name=window_name,
                        bucket_start_ms=bucket_start,
                        payload=_empty_closed_payload(bucket_start=bucket_start, bucket_end=bucket_end),
                    )
                    written += 1
                    written_starts.add(bucket_start)
                    if bucket_start > max_closed:
                        max_closed = bucket_start
            open_start = bucket_start_for_timestamp(now_ms, window_ms)
            if open_start not in populated_starts and open_start not in written_starts:
                bucket_end = open_start + window_ms
                payload = _empty_closed_payload(bucket_start=open_start, bucket_end=bucket_end)
                payload["time_bucket_closed"] = False
                payload["observation"] = "in_progress"
                await upsert_window_rollup(
                    session,
                    symbol=symbol,
                    source_exchange=source,
                    window_name=window_name,
                    bucket_start_ms=open_start,
                    payload=payload,
                )
                written += 1
            cursor.last_closed_bucket_start_ms = max_closed
    return written
