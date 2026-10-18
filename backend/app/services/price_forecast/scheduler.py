from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PriceForecastSettings
from app.services.price_forecast.maintenance import prune_forecast_storage, reconcile_stale_forecast_runs
from app.services.price_forecast.run_service import ForecastRunOutcome, execute_forecast_with_lock
from app.services.price_forecast.settings_service import get_or_create_forecast_settings

_prune_counter = 0


async def should_run_scheduled_forecast(session, cfg: PriceForecastSettings) -> bool:
    if not cfg.enabled:
        return False
    now = datetime.now(timezone.utc)
    if cfg.next_run_at is None:
        return True
    nxt = cfg.next_run_at.replace(tzinfo=timezone.utc) if cfg.next_run_at.tzinfo is None else cfg.next_run_at
    return now >= nxt


async def execute_scheduled_forecast_if_due() -> ForecastRunOutcome:
    global _prune_counter
    async with SessionLocal() as session:
        await reconcile_stale_forecast_runs(session)
        _prune_counter += 1
        if _prune_counter >= 60:
            _prune_counter = 0
            await prune_forecast_storage(session)
        await session.commit()
        cfg = await get_or_create_forecast_settings(session)
        if not await should_run_scheduled_forecast(session, cfg):
            return ForecastRunOutcome(status="skipped", message="not_due")
    return await execute_forecast_with_lock(trigger="scheduled")
