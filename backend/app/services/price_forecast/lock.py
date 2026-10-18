from __future__ import annotations

import time

from sqlalchemy import update

from app.config import get_settings
from app.db import SessionLocal
from app.models import SystemState
from app.services.system_state import get_system_state


def _now_ms() -> int:
    return int(time.time() * 1000)


async def acquire_price_forecast_lock(owner: str) -> bool:
    ttl_ms = int(get_settings().ai_analysis_lock_ttl_seconds) * 1000
    until = _now_ms() + ttl_ms
    async with SessionLocal() as session:
        await get_system_state(session)
        result = await session.execute(
            update(SystemState)
            .where(SystemState.id == 1)
            .where(
                (SystemState.price_forecast_lock_until_ms.is_(None))
                | (SystemState.price_forecast_lock_until_ms <= _now_ms())
                | (SystemState.price_forecast_lock_owner == owner)
            )
            .values(price_forecast_lock_owner=owner, price_forecast_lock_until_ms=until)
        )
        await session.commit()
        return bool(result.rowcount)


async def release_price_forecast_lock(owner: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(SystemState)
            .where(SystemState.id == 1)
            .where(SystemState.price_forecast_lock_owner == owner)
            .values(price_forecast_lock_owner=None, price_forecast_lock_until_ms=0)
        )
        await session.commit()
