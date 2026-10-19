from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, cast, delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import LiquidationEvent, LiquidationRollupCursor, LiquidationSourceStatus, LiquidationWindowRollup
from app.services.liquidation_intelligence.coverage import append_coverage_gap
from app.services.liquidation_intelligence.fetch_result import FetchEventsResult
from app.services.liquidation_intelligence.types import NormalizedLiquidation


async def ensure_source_status(session: AsyncSession, source: str) -> LiquidationSourceStatus:
    row = (
        await session.execute(
            select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == source)
        )
    ).scalar_one_or_none()
    if row is None:
        row = LiquidationSourceStatus(source_exchange=source)
        session.add(row)
        await session.flush()
    return row


async def record_source_message(
    session: AsyncSession,
    source: str,
    *,
    connected: bool,
    error: str | None = None,
    rejected_delta: int = 0,
    received_delta: int = 0,
    duplicate_delta: int = 0,
    reconnect_delta: int = 0,
    message_at_ms: int | None = None,
    heartbeat_at_ms: int | None = None,
    connection_activity_at_ms: int | None = None,
    subscription_confirmed: bool | None = None,
) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    st = await ensure_source_status(session, source)
    was_connected = bool(st.connected)
    if not connected and was_connected:
        start = int(st.connected_since_ms or 0)
        if start > 0:
            st.coverage_gaps = append_coverage_gap(st.coverage_gaps, start_ms=start, end_ms=now_ms, now_ms=now_ms)
        st.connected_since_ms = None
        st.subscription_confirmed = False
    if connected and not was_connected:
        st.connected_since_ms = now_ms
        if subscription_confirmed is None:
            st.subscription_confirmed = False
    st.connected = connected
    if message_at_ms is not None:
        st.last_message_at_ms = message_at_ms
        st.last_event_at_ms = message_at_ms
    activity_ms = connection_activity_at_ms or heartbeat_at_ms
    if message_at_ms is not None:
        activity_ms = activity_ms or now_ms
    if activity_ms is not None:
        st.last_connection_activity_at_ms = activity_ms
        st.last_heartbeat_at_ms = activity_ms
    elif heartbeat_at_ms is not None:
        st.last_heartbeat_at_ms = heartbeat_at_ms
        st.last_connection_activity_at_ms = heartbeat_at_ms
    if subscription_confirmed is not None:
        st.subscription_confirmed = subscription_confirmed
    st.events_received = int(st.events_received or 0) + received_delta
    st.events_rejected = int(st.events_rejected or 0) + rejected_delta
    st.duplicates = int(st.duplicates or 0) + duplicate_delta
    st.reconnect_count = int(st.reconnect_count or 0) + reconnect_delta
    if error:
        st.last_error = error[:255]
    elif connected and received_delta:
        st.last_error = None
    st.updated_at = datetime.now(timezone.utc)


async def insert_liquidation_event(session: AsyncSession, ev: NormalizedLiquidation) -> tuple[bool, bool]:
    """Returns (inserted, updated). Binance order snapshots upsert cumulative z without double-count rows."""
    row = ev.to_row()
    if ev.source_exchange == "binance" and ev.exchange_order_id:
        existing = (
            await session.execute(select(LiquidationEvent).where(LiquidationEvent.dedupe_key == ev.dedupe_key))
        ).scalar_one_or_none()
        if existing is not None:
            new_z = float(ev.cumulative_qty or ev.quantity_btc)
            old_z = float(existing.quantity_btc)
            if new_z <= old_z:
                return False, False
            existing.quantity_btc = new_z
            existing.price = float(ev.price)
            existing.estimated_notional_usd = float(ev.estimated_notional_usd)
            existing.event_timestamp_ms = int(ev.event_timestamp_ms)
            existing.received_timestamp_ms = int(ev.received_timestamp_ms)
            existing.source_quality = ev.source_quality
            return False, True
    stmt = sqlite_insert(LiquidationEvent).values(**row)
    stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    result = await session.execute(stmt)
    return bool(result.rowcount), False


async def fetch_events_since(
    session: AsyncSession,
    *,
    symbol: str,
    since_ms: int,
    source: str | None = None,
    limit: int = 50_000,
) -> FetchEventsResult:
    """Fetch newest events in range (cap at limit) in chronological order for forecast analytics."""
    filters = [
        LiquidationEvent.symbol == symbol,
        LiquidationEvent.event_timestamp_ms >= since_ms,
    ]
    if source:
        filters.append(LiquidationEvent.source_exchange == source)
    q = (
        select(LiquidationEvent)
        .where(*filters)
        .order_by(desc(LiquidationEvent.event_timestamp_ms))
        .limit(limit)
    )
    rows = list((await session.execute(q)).scalars().all())
    rows.reverse()
    earliest = int(rows[0].event_timestamp_ms) if rows else None
    truncated = False
    matching: int | None = None
    if len(rows) >= limit:
        matching = int((await session.execute(select(func.count()).select_from(LiquidationEvent).where(*filters))).scalar_one())
        truncated = matching > limit
    if not truncated and earliest is not None and earliest > since_ms and len(rows) >= limit:
        truncated = True
    return FetchEventsResult(
        events=rows,
        truncated=truncated,
        row_limit=limit,
        rows_returned=len(rows),
        since_ms=since_ms,
        earliest_returned_ms=earliest,
        matching_row_count=matching,
    )


async def latest_event_time(session: AsyncSession, symbol: str) -> int | None:
    q = (
        select(func.max(LiquidationEvent.event_timestamp_ms))
        .where(LiquidationEvent.symbol == symbol)
    )
    val = (await session.execute(q)).scalar_one_or_none()
    return int(val) if val else None


async def upsert_window_rollup(
    session: AsyncSession,
    *,
    symbol: str,
    source_exchange: str,
    window_name: str,
    bucket_start_ms: int,
    payload: dict,
) -> None:
    existing = (
        await session.execute(
            select(LiquidationWindowRollup).where(
                LiquidationWindowRollup.symbol == symbol,
                LiquidationWindowRollup.source_exchange == source_exchange,
                LiquidationWindowRollup.window_name == window_name,
                LiquidationWindowRollup.bucket_start_ms == bucket_start_ms,
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.payload = payload
    else:
        session.add(
            LiquidationWindowRollup(
                symbol=symbol,
                source_exchange=source_exchange,
                window_name=window_name,
                bucket_start_ms=bucket_start_ms,
                payload=payload,
            )
        )


async def prune_liquidation_data(session: AsyncSession) -> tuple[int, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    raw_cutoff_ms = int((now - timedelta(days=int(settings.liquidation_raw_retention_days))).timestamp() * 1000)
    roll_cutoff_ms = int((now - timedelta(days=int(settings.liquidation_rollup_retention_days))).timestamp() * 1000)
    ev_res = await session.execute(delete(LiquidationEvent).where(LiquidationEvent.event_timestamp_ms < raw_cutoff_ms))
    roll_res = await session.execute(
        delete(LiquidationWindowRollup).where(LiquidationWindowRollup.bucket_start_ms < roll_cutoff_ms)
    )
    return int(ev_res.rowcount or 0), int(roll_res.rowcount or 0)


async def all_source_status(session: AsyncSession) -> list[LiquidationSourceStatus]:
    return list((await session.execute(select(LiquidationSourceStatus).order_by(LiquidationSourceStatus.source_exchange))).scalars().all())


async def get_rollup_cursor(
    session: AsyncSession,
    *,
    symbol: str,
    window_name: str,
    source_exchange: str,
) -> LiquidationRollupCursor:
    row = (
        await session.execute(
            select(LiquidationRollupCursor).where(
                LiquidationRollupCursor.symbol == symbol,
                LiquidationRollupCursor.window_name == window_name,
                LiquidationRollupCursor.source_exchange == source_exchange,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = LiquidationRollupCursor(symbol=symbol, window_name=window_name, source_exchange=source_exchange)
        session.add(row)
        await session.flush()
    return row


async def aggregate_liquidation_buckets(
    session: AsyncSession,
    *,
    symbol: str,
    window_ms: int,
    since_ms: int,
    until_ms: int,
    source_exchange: str,
) -> list[dict]:
    """Single-pass SQL grouping — no row limit; bucket_start <= ts < bucket_end by construction."""
    bucket = (cast(LiquidationEvent.event_timestamp_ms / window_ms, Integer) * window_ms).label("bucket_start")
    long_sum = func.sum(
        case(
            (LiquidationEvent.liquidated_position_side == "long", LiquidationEvent.estimated_notional_usd),
            else_=0.0,
        )
    ).label("long_notional")
    short_sum = func.sum(
        case(
            (LiquidationEvent.liquidated_position_side == "short", LiquidationEvent.estimated_notional_usd),
            else_=0.0,
        )
    ).label("short_notional")
    cnt = func.count(LiquidationEvent.id).label("event_count")
    largest = func.max(LiquidationEvent.estimated_notional_usd).label("largest")
    q = (
        select(bucket, long_sum, short_sum, cnt, largest)
        .where(
            LiquidationEvent.symbol == symbol,
            LiquidationEvent.event_timestamp_ms >= since_ms,
            LiquidationEvent.event_timestamp_ms < until_ms,
        )
        .group_by(bucket)
        .order_by(bucket)
    )
    if source_exchange != "combined":
        q = q.where(LiquidationEvent.source_exchange == source_exchange)
    rows = (await session.execute(q)).all()
    out: list[dict] = []
    for row in rows:
        long_n = float(row.long_notional or 0)
        short_n = float(row.short_notional or 0)
        total = long_n + short_n
        ratio = (long_n / short_n) if short_n > 0 else None
        out.append(
            {
                "bucket_start_ms": int(row.bucket_start),
                "long_liquidation_notional_usd": round(long_n, 2),
                "short_liquidation_notional_usd": round(short_n, 2),
                "total_observed_notional_usd": round(total, 2),
                "long_short_ratio": round(ratio, 4) if ratio is not None else None,
                "event_count": int(row.event_count or 0),
                "largest_observed_usd": round(float(row.largest or 0), 2),
            }
        )
    return out
