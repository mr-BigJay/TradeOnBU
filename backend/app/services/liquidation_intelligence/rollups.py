from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.liquidation_intelligence.analytics import WINDOW_MS, _Acc
from app.services.liquidation_intelligence.coverage import events_in_aligned_bucket
from app.services.liquidation_intelligence.repository import fetch_events_since, upsert_window_rollup


def bucket_start_for_timestamp(timestamp_ms: int, window_ms: int) -> int:
    return (int(timestamp_ms) // window_ms) * window_ms


def bucket_bounds(bucket_start_ms: int, window_ms: int) -> tuple[int, int]:
    return bucket_start_ms, bucket_start_ms + window_ms


def collect_bucket_starts_from_events(events: list[Any], window_ms: int, *, through_ms: int) -> set[int]:
    starts: set[int] = set()
    for ev in events:
        t = int(ev.event_timestamp_ms)
        if t <= 0:
            continue
        starts.add(bucket_start_for_timestamp(t, window_ms))
    # Include in-progress bucket at through_ms so hourly job refreshes open bucket.
    starts.add(bucket_start_for_timestamp(through_ms, window_ms))
    return starts


async def materialize_rollups_for_symbol(
    session: AsyncSession,
    *,
    symbol: str = "BTCUSDT",
    now_ms: int,
    sources: tuple[str, ...] = ("bybit", "binance", "combined"),
) -> int:
    """Idempotent, time-aligned rollups: bucket_start <= event_ts < bucket_end."""
    settings = get_settings()
    raw_cutoff_ms = now_ms - int(settings.liquidation_raw_retention_days) * 86400 * 1000
    all_events = await fetch_events_since(session, symbol=symbol, since_ms=raw_cutoff_ms)
    written = 0
    for window_name, window_ms in WINDOW_MS.items():
        bucket_starts = collect_bucket_starts_from_events(all_events, window_ms, through_ms=now_ms)
        for bucket_start in sorted(bucket_starts):
            bucket_end = bucket_start + window_ms
            for source in sources:
                if source == "combined":
                    pool = all_events
                else:
                    pool = [e for e in all_events if str(e.source_exchange) == source]
                evs = events_in_aligned_bucket(pool, bucket_start, bucket_end)
                acc = _Acc()
                for ev in evs:
                    acc.add(str(ev.liquidated_position_side), float(ev.estimated_notional_usd))
                payload: dict[str, Any] = acc.to_dict()
                payload["bucket_start_ms"] = bucket_start
                payload["bucket_end_ms"] = bucket_end
                payload["event_count"] = len(evs)
                payload["bucket_complete"] = now_ms >= bucket_end
                payload["coverage"] = "complete" if now_ms >= bucket_end else "in_progress"
                if not evs and now_ms >= bucket_end:
                    payload["coverage"] = "empty_complete"
                if not math.isfinite(payload.get("total_observed_notional_usd") or 0):
                    payload["total_observed_notional_usd"] = 0.0
                await upsert_window_rollup(
                    session,
                    symbol=symbol,
                    source_exchange=source,
                    window_name=window_name,
                    bucket_start_ms=bucket_start,
                    payload=payload,
                )
                written += 1
    return written
