from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceForecastRun, PriceForecastSettings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_forecast_settings(session: AsyncSession) -> PriceForecastSettings:
    row = (await session.execute(select(PriceForecastSettings).where(PriceForecastSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = PriceForecastSettings(id=1, enabled=False, interval_minutes=60)
        session.add(row)
        await session.flush()
    return row


async def count_runs_today(session: AsyncSession) -> int:
    start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count()).select_from(PriceForecastRun).where(
        PriceForecastRun.created_at >= start,
        PriceForecastRun.status.in_(("ok", "failed")),
    )
    return int((await session.execute(q)).scalar_one())


async def month_cost_usd(session: AsyncSession) -> float:
    now = _utc_now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = select(func.coalesce(func.sum(PriceForecastRun.estimated_cost_usd), 0.0)).where(
        PriceForecastRun.created_at >= start,
        PriceForecastRun.status == "ok",
    )
    return float((await session.execute(q)).scalar_one())


def schedule_next_run(settings: PriceForecastSettings, *, from_dt: datetime | None = None) -> datetime:
    now = from_dt or _utc_now()
    interval = max(15, int(settings.interval_minutes or 60))
    if settings.last_success_at:
        base = settings.last_success_at.replace(tzinfo=timezone.utc) if settings.last_success_at.tzinfo is None else settings.last_success_at
        return max(base + timedelta(minutes=interval), now)
    return now + timedelta(minutes=interval)
