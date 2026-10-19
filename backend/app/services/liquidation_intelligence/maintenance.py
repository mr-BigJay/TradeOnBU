from __future__ import annotations

from app.db import SessionLocal
from app.services.liquidation_intelligence.repository import prune_liquidation_data


async def run_liquidation_retention_tick() -> tuple[int, int]:
    async with SessionLocal() as session:
        deleted = await prune_liquidation_data(session)
        await session.commit()
        return deleted
