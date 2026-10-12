from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemState


async def get_system_state(session: AsyncSession) -> SystemState:
    row = (await session.execute(select(SystemState).where(SystemState.id == 1))).scalar_one_or_none()
    if row is None:
        row = SystemState(id=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def try_acquire_worker_lock(session: AsyncSession, owner: str, ttl_seconds: int = 900) -> bool:
    state = await get_system_state(session)
    now = datetime.now(timezone.utc)
    if state.worker_lock_until and state.worker_lock_until > now and state.worker_lock_owner not in (None, owner):
        return False
    state.worker_lock_owner = owner
    state.worker_lock_until = now + timedelta(seconds=ttl_seconds)
    await session.commit()
    return True


async def release_worker_lock(session: AsyncSession, owner: str) -> None:
    state = await get_system_state(session)
    if state.worker_lock_owner == owner:
        state.worker_lock_owner = None
        state.worker_lock_until = None
        await session.commit()
