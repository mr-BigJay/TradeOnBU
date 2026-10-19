from __future__ import annotations

import time

from app.db import SessionLocal
from app.services.liquidation_intelligence.repository import prune_liquidation_data
from app.services.liquidation_intelligence.rollups import materialize_rollups_for_symbol


async def run_liquidation_retention_tick() -> tuple[int, int, int]:
    async with SessionLocal() as session:
        now_ms = int(time.time() * 1000)
        rollups = await materialize_rollups_for_symbol(session, now_ms=now_ms)
        deleted = await prune_liquidation_data(session)
        await session.commit()
        return deleted[0], deleted[1], rollups
