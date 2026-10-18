from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import MarketDataSnapshot, MarketMicroRollup, PriceForecastRun, PriceForecastSettings

SNAPSHOT_RETENTION_DAYS = 30
MICRO_ROLLUP_RETENTION_DAYS = 14


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def schedule_after_daily_limit_reached(*, now: datetime | None = None) -> datetime:
    now = now or _utc_now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def schedule_after_monthly_budget_reached(*, now: datetime | None = None) -> datetime:
    now = now or _utc_now()
    if now.month == 12:
        first_next = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        first_next = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(first_next, now + timedelta(hours=1))


async def reconcile_stale_forecast_runs(session: AsyncSession) -> int:
    """Mark orphaned in-flight runs failed so daily limits and UI status recover after crash."""
    ttl = int(get_settings().ai_analysis_lock_ttl_seconds)
    cutoff = _utc_now() - timedelta(seconds=ttl + 120)
    result = await session.execute(
        update(PriceForecastRun)
        .where(PriceForecastRun.status == "running")
        .where(PriceForecastRun.created_at < cutoff)
        .values(
            status="failed",
            error_code="process_interrupted",
            error_message="Forecast run interrupted (stale running state)",
        )
    )
    n = int(result.rowcount or 0)
    if n:
        cfg = (
            await session.execute(select(PriceForecastSettings).where(PriceForecastSettings.id == 1))
        ).scalar_one_or_none()
        if cfg and cfg.running_status == "running":
            cfg.running_status = "idle"
            cfg.last_error = cfg.last_error or "process_interrupted"
    return n


async def prune_forecast_storage(session: AsyncSession) -> tuple[int, int]:
    now = _utc_now()
    snap_cutoff = now - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    roll_cutoff_ms = int((now - timedelta(days=MICRO_ROLLUP_RETENTION_DAYS)).timestamp() * 1000)

    snap_res = await session.execute(delete(MarketDataSnapshot).where(MarketDataSnapshot.collected_at < snap_cutoff))
    roll_res = await session.execute(
        delete(MarketMicroRollup).where(MarketMicroRollup.bucket_start_ms < roll_cutoff_ms)
    )
    return int(snap_res.rowcount or 0), int(roll_res.rowcount or 0)
