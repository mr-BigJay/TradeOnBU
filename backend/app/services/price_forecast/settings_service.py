from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceForecastRun, PriceForecastSettings

LUNA_STANDARD_INPUT_MT = 0.10
LUNA_STANDARD_CACHED_MT = 0.01
LUNA_STANDARD_OUTPUT_MT = 0.50


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def apply_luna_standard_pricing_defaults(cfg: PriceForecastSettings) -> None:
    """One-time alignment for rows created before pricing fix."""
    if float(cfg.input_price_per_mtok) >= 1.0:
        cfg.input_price_per_mtok = LUNA_STANDARD_INPUT_MT
        cfg.cached_input_price_per_mtok = LUNA_STANDARD_CACHED_MT
        cfg.output_price_per_mtok = LUNA_STANDARD_OUTPUT_MT
        cfg.reasoning_price_per_mtok = 0.0


async def get_or_create_forecast_settings(session: AsyncSession) -> PriceForecastSettings:
    row = (await session.execute(select(PriceForecastSettings).where(PriceForecastSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = PriceForecastSettings(id=1, enabled=False, interval_minutes=60)
        session.add(row)
        await session.flush()
    apply_luna_standard_pricing_defaults(row)
    return row


async def count_billable_runs_today(session: AsyncSession) -> int:
    """Every persisted run that reached OpenAI billing (ok/failed/running)."""
    start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count()).select_from(PriceForecastRun).where(
        PriceForecastRun.created_at >= start,
        PriceForecastRun.status.in_(("ok", "failed", "running")),
    )
    return int((await session.execute(q)).scalar_one())


async def month_cost_usd(session: AsyncSession) -> float:
    now = _utc_now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = select(func.coalesce(func.sum(PriceForecastRun.estimated_cost_usd), 0.0)).where(
        PriceForecastRun.created_at >= start,
        PriceForecastRun.status.in_(("ok", "failed")),
    )
    return float((await session.execute(q)).scalar_one())


def schedule_next_run(settings: PriceForecastSettings, *, from_dt: datetime | None = None) -> datetime:
    now = from_dt or _utc_now()
    interval = max(15, int(settings.interval_minutes or 60))
    if settings.last_success_at:
        base = settings.last_success_at.replace(tzinfo=timezone.utc) if settings.last_success_at.tzinfo is None else settings.last_success_at
        return max(base + timedelta(minutes=interval), now)
    return now + timedelta(minutes=interval)
