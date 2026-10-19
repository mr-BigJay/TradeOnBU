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


async def materialize_rollups_for_symbol(
    session: AsyncSession,
    *,
    symbol: str = "BTCUSDT",
    now_ms: int,
    sources: tuple[str, ...] = ("bybit", "binance", "combined"),
) -> int:
    """Incremental SQL rollups: one grouped query per window/source; idempotent upserts."""
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
            seen_starts = {int(a["bucket_start_ms"]) for a in aggregates}
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
                }
                if time_bucket_closed and agg.get("event_count", 0) == 0:
                    payload["observation"] = "empty_complete"
                elif not time_bucket_closed:
                    payload["observation"] = "in_progress"
                else:
                    payload["observation"] = "complete"
                await upsert_window_rollup(
                    session,
                    symbol=symbol,
                    source_exchange=source,
                    window_name=window_name,
                    bucket_start_ms=bucket_start,
                    payload=payload,
                )
                written += 1
                if time_bucket_closed and bucket_start > max_closed:
                    max_closed = bucket_start
            open_start = bucket_start_for_timestamp(now_ms, window_ms)
            if open_start not in seen_starts:
                bucket_end = open_start + window_ms
                payload = {
                    "bucket_start_ms": open_start,
                    "bucket_end_ms": bucket_end,
                    "long_liquidation_notional_usd": 0.0,
                    "short_liquidation_notional_usd": 0.0,
                    "total_observed_notional_usd": 0.0,
                    "long_short_ratio": None,
                    "event_count": 0,
                    "largest_observed_usd": 0.0,
                    "time_bucket_closed": False,
                    "connection_coverage_complete": None,
                    "observation": "in_progress",
                    "rollup_kind": "time_aligned_event_sum",
                }
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
