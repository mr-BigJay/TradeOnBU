from __future__ import annotations

import time
import uuid

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemState
from app.services.system_state import get_system_state

_LOCK_TTL_SECONDS = 55


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_monitor_owner() -> str:
    return f"opp-mon-{uuid.uuid4().hex[:10]}"


async def try_acquire_opportunity_monitor_lock(session: AsyncSession, owner: str) -> bool:
    await get_system_state(session)
    now_ms = _now_ms()
    until_ms = now_ms + _LOCK_TTL_SECONDS * 1000
    stmt = (
        update(SystemState)
        .where(SystemState.id == 1)
        .where(
            or_(
                SystemState.opportunity_monitor_lock_until_ms.is_(None),
                SystemState.opportunity_monitor_lock_until_ms <= now_ms,
                SystemState.opportunity_monitor_lock_owner == owner,
            )
        )
        .values(
            opportunity_monitor_lock_owner=owner,
            opportunity_monitor_lock_until_ms=until_ms,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    if result.rowcount != 1:
        state = await get_system_state(session)
        return state.opportunity_monitor_lock_owner == owner and (state.opportunity_monitor_lock_until_ms or 0) > now_ms
    state = await get_system_state(session)
    return state.opportunity_monitor_lock_owner == owner


async def release_opportunity_monitor_lock(session: AsyncSession, owner: str) -> None:
    state = await get_system_state(session)
    if state.opportunity_monitor_lock_owner == owner:
        state.opportunity_monitor_lock_owner = None
        state.opportunity_monitor_lock_until_ms = 0
        await session.flush()
