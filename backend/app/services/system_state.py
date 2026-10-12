from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.time_utils import ensure_utc
from app.models import SystemState


def _now_ms() -> int:
    return int(time.time() * 1000)


async def get_system_state(session: AsyncSession) -> SystemState:
    row = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    if row is None:
        row = SystemState(id=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def try_acquire_worker_lock(session: AsyncSession, owner: str, ttl_seconds: int = 900) -> bool:
    await get_system_state(session)
    now_ms = _now_ms()
    until_ms = now_ms + ttl_seconds * 1000
    stmt = (
        update(SystemState)
        .where(SystemState.id == 1)
        .where(
            or_(
                SystemState.worker_lock_until_ms.is_(None),
                SystemState.worker_lock_until_ms <= now_ms,
                SystemState.worker_lock_owner == owner,
            )
        )
        .values(
            worker_lock_owner=owner,
            worker_lock_until_ms=until_ms,
            worker_lock_until=datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc),
        )
    )
    await session.execute(stmt)
    await session.commit()
    state = await get_system_state(session)
    if state.worker_lock_owner != owner:
        return False
    return (state.worker_lock_until_ms or 0) > now_ms


async def release_worker_lock(session: AsyncSession, owner: str) -> None:
    state = await get_system_state(session)
    if state.worker_lock_owner == owner:
        state.worker_lock_owner = None
        state.worker_lock_until = None
        state.worker_lock_until_ms = 0
        await session.commit()
