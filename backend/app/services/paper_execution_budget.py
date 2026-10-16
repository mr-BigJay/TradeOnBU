"""Restart-safe, concurrency-safe caps for automatic paper entry orders."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paper_execution_policy import (
    MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
)
from app.domain.tehran_time import report_period_bounds_utc, tehran_now
from app.models import SimPosition
from app.services.system_state import get_system_state


def _is_auto_paper_position(extra: dict | None) -> bool:
    return bool((extra or {}).get("auto_paper"))


async def _maybe_begin_immediate(session: AsyncSession) -> None:
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite" and not session.in_transaction():
        await session.execute(text("BEGIN IMMEDIATE"))


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


async def try_allocate_auto_paper_budget(session: AsyncSession) -> tuple[bool, str | None, dict]:
    """
    Atomically reserve one concurrent auto-paper slot and one Tehran-day order slot.
    Must be paired with rollback_allocate on failure or release_active_slot on terminal position.
    """
    await _maybe_begin_immediate(session)
    today = tehran_now().date().isoformat()
    state = await get_system_state(session)
    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    }

    if state.auto_paper_tehran_day != today:
        state.auto_paper_tehran_day = today
        state.auto_paper_orders_today = 0
        state.auto_paper_active_slots = await count_active_auto_paper_entries(session)

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


async def rollback_allocate_auto_paper_budget(session: AsyncSession) -> None:
    """Order was not created — undo reservation."""
    state = await get_system_state(session)
    if (state.auto_paper_orders_today or 0) > 0:
        state.auto_paper_orders_today -= 1
    if (state.auto_paper_active_slots or 0) > 0:
        state.auto_paper_active_slots -= 1
    await session.flush()


async def release_auto_paper_active_slot(session: AsyncSession) -> None:
    """Pending/open auto paper position reached terminal state — free concurrent slot only."""
    state = await get_system_state(session)
    if (state.auto_paper_active_slots or 0) > 0:
        state.auto_paper_active_slots -= 1
        await session.flush()


async def auto_paper_budget_allows_new_entry(session: AsyncSession) -> tuple[bool, str | None, dict]:
    """Read-only budget view (metrics); order path uses try_allocate_auto_paper_budget."""
    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    }
    active = await count_active_auto_paper_entries(session)
    meta["active_auto_paper"] = active
    today_count = await count_auto_paper_entry_orders_tehran_today(session)
    meta["auto_paper_orders_tehran_today"] = today_count
    if active >= MAX_CONCURRENT_AUTO_PAPER_ENTRIES:
        return False, "max_concurrent_auto_paper", meta
    if today_count >= MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY:
        return False, "daily_auto_paper_cap_tehran", meta
    return True, None, meta
