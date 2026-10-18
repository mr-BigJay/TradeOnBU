from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PriceForecastSettings
from app.services.price_forecast.run_service import ForecastRunOutcome, execute_forecast_with_lock
from app.services.price_forecast.settings_service import get_or_create_forecast_settings


async def should_run_scheduled_forecast(session, cfg: PriceForecastSettings) -> bool:
    if not cfg.enabled:
        return False
    now = datetime.now(timezone.utc)
    if cfg.next_run_at is None:
        return True
    nxt = cfg.next_run_at.replace(tzinfo=timezone.utc) if cfg.next_run_at.tzinfo is None else cfg.next_run_at
    return now >= nxt


async def execute_scheduled_forecast_if_due() -> ForecastRunOutcome:
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        if not await should_run_scheduled_forecast(session, cfg):
            return ForecastRunOutcome(status="skipped", message="not_due")
    return await execute_forecast_with_lock(trigger="scheduled")
