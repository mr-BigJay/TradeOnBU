from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import LiquidationEvent, LiquidationSourceStatus, LiquidationWindowRollup
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
    subscription_confirmed: bool | None = None,
) -> None:
    st = await ensure_source_status(session, source)
    st.connected = connected
    if message_at_ms is not None:
        st.last_message_at_ms = message_at_ms
        st.last_event_at_ms = message_at_ms
    if heartbeat_at_ms is not None:
        st.last_heartbeat_at_ms = heartbeat_at_ms
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
) -> list[LiquidationEvent]:
    """Fetch newest events in range (cap at limit) in chronological order for analytics."""
    q = (
        select(LiquidationEvent)
        .where(LiquidationEvent.symbol == symbol, LiquidationEvent.event_timestamp_ms >= since_ms)
        .order_by(desc(LiquidationEvent.event_timestamp_ms))
        .limit(limit)
    )
    if source:
        q = q.where(LiquidationEvent.source_exchange == source)
    rows = list((await session.execute(q)).scalars().all())
    rows.reverse()
    return rows


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
