"""Restart-safe caps for automatic paper entry orders (Tehran calendar day)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paper_execution_policy import (
    MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
)
from app.domain.tehran_time import report_period_bounds_utc
from app.models import SimPosition


def _is_auto_paper_position(extra: dict | None) -> bool:
    return bool((extra or {}).get("auto_paper"))


async def count_active_auto_paper_entries(session: AsyncSession) -> int:
    """Pending or open positions created by auto paper (any symbol)."""
    result = await session.execute(
        select(func.count())
        .select_from(SimPosition)
        .where(SimPosition.status.in_(("pending_entry", "open")))
    )
    total_active = int(result.scalar_one())
    if total_active == 0:
        return 0
    rows = (
        await session.execute(
            select(SimPosition.extra).where(SimPosition.status.in_(("pending_entry", "open")))
        )
    ).all()
    return sum(1 for (extra,) in rows if _is_auto_paper_position(extra))


async def count_auto_paper_entry_orders_tehran_today(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Auto paper orders created since Asia/Tehran midnight (includes cancelled unfilled)."""
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


async def auto_paper_budget_allows_new_entry(session: AsyncSession) -> tuple[bool, str | None, dict]:
    """
    Enforce max one concurrent auto entry and max two auto entry orders per Tehran day.
    """
    meta: dict = {
        "max_concurrent": MAX_CONCURRENT_AUTO_PAPER_ENTRIES,
        "max_per_tehran_day": MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
    }
    active = await count_active_auto_paper_entries(session)
    meta["active_auto_paper"] = active
    if active >= MAX_CONCURRENT_AUTO_PAPER_ENTRIES:
        return False, "max_concurrent_auto_paper", meta

    today_count = await count_auto_paper_entry_orders_tehran_today(session)
    meta["auto_paper_orders_tehran_today"] = today_count
    if today_count >= MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY:
        return False, "daily_auto_paper_cap_tehran", meta
    return True, None, meta
