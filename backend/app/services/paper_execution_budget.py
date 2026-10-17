"""Restart-safe, concurrency-safe caps for automatic paper entry orders."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.domain.paper_execution_policy import (
    MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
)
from app.domain.tehran_time import report_period_bounds_utc, tehran_now
from app.models import SimPosition
from app.services.system_state import get_system_state


def _is_auto_paper_position(extra: dict | None) -> bool:
    return bool((extra or {}).get("auto_paper"))


async def count_active_auto_paper_entries(session: AsyncSession) -> int:
    rows = (
        await session.execute(
            select(SimPosition.extra).where(SimPosition.status.in_(("pending_entry", "open")))
        )
    ).all()
    return sum(1 for (extra,) in rows if _is_auto_paper_position(extra))


async def count_auto_paper_entry_orders_tehran_today(session: AsyncSession, *, now: datetime | None = None) -> int:
    start, end = report_period_bounds_utc("today")
    if start is None or end is None:
        return 0
    now = now or datetime.now(timezone.utc)
    end = min(end, now if now.tzinfo else now.replace(tzinfo=timezone.utc))
    rows = (
        await session.execute(
            select(SimPosition.extra, SimPosition.created_at).where(
                SimPosition.created_at >= start,
                SimPosition.created_at <= end,
            )
        )
    ).all()
    return sum(1 for extra, _ in rows if _is_auto_paper_position(extra))


async def reconcile_auto_paper_budget_from_db(session: AsyncSession) -> None:
    """Align SystemState counters with persisted auto-paper orders (day rollover / first deploy)."""
    today = tehran_now().date().isoformat()
    state = await get_system_state(session)
    orders = await count_auto_paper_entry_orders_tehran_today(session)
    active = await count_active_auto_paper_entries(session)
    state.auto_paper_tehran_day = today
    state.auto_paper_orders_today = orders
    state.auto_paper_active_slots = active
    await session.flush()


async def _allocate_on_session(session: AsyncSession) -> tuple[bool, str | None, dict]:
    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    }
    today = tehran_now().date().isoformat()
    state = await get_system_state(session)
    if state.auto_paper_tehran_day != today or state.auto_paper_tehran_day is None:
        await reconcile_auto_paper_budget_from_db(session)
        state = await get_system_state(session)

    meta["active_auto_paper"] = int(state.auto_paper_active_slots or 0)
    meta["auto_paper_orders_tehran_today"] = int(state.auto_paper_orders_today or 0)

    if (state.auto_paper_active_slots or 0) >= MAX_CONCURRENT_AUTO_PAPER_ENTRIES:
        return False, "max_concurrent_auto_paper", meta
    if (state.auto_paper_orders_today or 0) >= MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY:
        return False, "daily_auto_paper_cap_tehran", meta

    state.auto_paper_active_slots = int(state.auto_paper_active_slots or 0) + 1
    state.auto_paper_orders_today = int(state.auto_paper_orders_today or 0) + 1
    await session.flush()
    meta["active_auto_paper"] = state.auto_paper_active_slots
    meta["auto_paper_orders_tehran_today"] = state.auto_paper_orders_today
    return True, None, meta


async def try_allocate_auto_paper_budget(session: AsyncSession | None = None) -> tuple[bool, str | None, dict]:
    """
    Reserve one concurrent auto-paper slot and one Tehran-day order slot.
    When called inside an open transaction, increments on that session (monitor savepoint).
    Otherwise uses an isolated SQLite IMMEDIATE transaction for cross-worker safety.
    """
    if session is not None and session.in_transaction():
        return await _allocate_on_session(session)

    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    }
    iso = SessionLocal()
    try:
        if iso.get_bind() and iso.get_bind().dialect.name == "sqlite":
            await iso.execute(text("PRAGMA busy_timeout=30000"))
            await iso.execute(text("BEGIN IMMEDIATE"))
        ok, reason, meta = await _allocate_on_session(iso)
        if not ok:
            await iso.rollback()
            return False, reason, meta
        await iso.commit()
        return True, None, meta
    except Exception:
        await iso.rollback()
        raise
    finally:
        await iso.close()


async def _rollback_on_session(session: AsyncSession) -> None:
    state = await get_system_state(session)
    today = tehran_now().date().isoformat()
    if state.auto_paper_tehran_day != today:
        await reconcile_auto_paper_budget_from_db(session)
        state = await get_system_state(session)
    if (state.auto_paper_orders_today or 0) > 0:
        state.auto_paper_orders_today -= 1
    if (state.auto_paper_active_slots or 0) > 0:
        state.auto_paper_active_slots -= 1
    await session.flush()


async def rollback_allocate_auto_paper_budget(session: AsyncSession | None = None) -> None:
    """Order was not created — undo reservation."""
    if session is not None and session.in_transaction():
        await _rollback_on_session(session)
        return
    iso = SessionLocal()
    try:
        if iso.get_bind() and iso.get_bind().dialect.name == "sqlite":
            await iso.execute(text("PRAGMA busy_timeout=30000"))
            await iso.execute(text("BEGIN IMMEDIATE"))
        await _rollback_on_session(iso)
        await iso.commit()
    except Exception:
        await iso.rollback()
        raise
    finally:
        await iso.close()


async def _release_active_on_session(session: AsyncSession) -> None:
    state = await get_system_state(session)
    today = tehran_now().date().isoformat()
    if state.auto_paper_tehran_day != today:
        await reconcile_auto_paper_budget_from_db(session)
        state = await get_system_state(session)
    if (state.auto_paper_active_slots or 0) > 0:
        state.auto_paper_active_slots -= 1
    await session.flush()


async def release_auto_paper_active_slot(session: AsyncSession | None = None) -> None:
    """Pending/open auto paper position reached terminal state — free concurrent slot only."""
    if session is not None and session.in_transaction():
        await _release_active_on_session(session)
        return
    iso = SessionLocal()
    try:
        if iso.get_bind() and iso.get_bind().dialect.name == "sqlite":
            await iso.execute(text("PRAGMA busy_timeout=30000"))
            await iso.execute(text("BEGIN IMMEDIATE"))
        await _release_active_on_session(iso)
        await iso.commit()
    except Exception:
        await iso.rollback()
        raise
    finally:
        await iso.close()


async def auto_paper_budget_allows_new_entry(session: AsyncSession) -> tuple[bool, str | None, dict]:
    """Read-only budget view (metrics)."""
    await reconcile_auto_paper_budget_from_db(session)
    state = await get_system_state(session)
    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
        "active_auto_paper": int(state.auto_paper_active_slots or 0),
        "auto_paper_orders_tehran_today": int(state.auto_paper_orders_today or 0),
    }
    if (state.auto_paper_active_slots or 0) >= MAX_CONCURRENT_AUTO_PAPER_ENTRIES:
        return False, "max_concurrent_auto_paper", meta
    if (state.auto_paper_orders_today or 0) >= MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY:
        return False, "daily_auto_paper_cap_tehran", meta
    return True, None, meta
