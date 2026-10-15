from __future__ import annotations

import time
import uuid

from sqlalchemy import or_, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import SystemState
from app.services.system_state import get_system_state

_LOCK_TTL_SECONDS = 55


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_monitor_owner() -> str:
    return f"opp-mon-{uuid.uuid4().hex[:10]}"


async def refresh_opportunity_monitor_lock(owner: str, session: AsyncSession | None = None) -> bool:
    """
    Extend monitor lease. When ``session`` is provided, flush only (same business transaction).
    Acquire/release always use isolated sessions so they never commit setup/order work.
    """
    now_ms = _now_ms()
    until_ms = now_ms + _LOCK_TTL_SECONDS * 1000
    stmt = (
        update(SystemState)
        .where(SystemState.id == 1)
        .where(SystemState.opportunity_monitor_lock_owner == owner)
        .values(opportunity_monitor_lock_until_ms=until_ms)
    )

    async def _apply(s: AsyncSession) -> bool:
        await get_system_state(s)
        result = await s.execute(stmt)
        return result.rowcount == 1

    if session is not None:
        return await _apply(session)

    async with SessionLocal() as isolated:
        ok = await _apply(isolated)
        await isolated.commit()
        return ok


async def try_acquire_opportunity_monitor_lock(owner: str) -> bool:
    async with SessionLocal() as session:
        if session.bind and session.bind.dialect.name == "sqlite":
            await session.execute(text("PRAGMA busy_timeout=250"))
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
        try:
            result = await session.execute(stmt)
        except OperationalError as exc:
            if session.bind and session.bind.dialect.name == "sqlite" and "locked" in str(exc).lower():
                return False
            raise
        await session.commit()
        if result.rowcount != 1:
            state = await get_system_state(session)
            return state.opportunity_monitor_lock_owner == owner and (state.opportunity_monitor_lock_until_ms or 0) > now_ms
        state = await get_system_state(session)
        return state.opportunity_monitor_lock_owner == owner


async def release_opportunity_monitor_lock(owner: str) -> None:
    async with SessionLocal() as session:
        if session.bind and session.bind.dialect.name == "sqlite":
            await session.execute(text("PRAGMA busy_timeout=250"))
        state = await get_system_state(session)
        if state.opportunity_monitor_lock_owner == owner:
            state.opportunity_monitor_lock_owner = None
            state.opportunity_monitor_lock_until_ms = 0
            await session.commit()
