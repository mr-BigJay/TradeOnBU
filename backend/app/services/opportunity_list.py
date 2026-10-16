"""Server-side tab filters and pagination for ICT opportunities dashboard."""

from __future__ import annotations

import math
from typing import Any, Literal

from sqlalchemy import desc, func, or_, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.setup_lifecycle import (
    EXPIRED,
    ICT_CONFIRMED_LONG,
    ICT_CONFIRMED_SHORT,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_CLOSED,
    POSITION_OPEN,
    READY_LONG,
    READY_SHORT,
    WAIT_LONG,
    WAIT_SHORT,
)
from app.models import AiAnalysisRun, AiProposedSetup, SimPosition

PageSize = Literal[20]
DEFAULT_PAGE_SIZE = 20

TAB_WAIT = "wait"
TAB_READY = "ready"
TAB_BLOCKED = "blocked"
TAB_INVALIDATED = "invalidated"
TAB_EXPIRED = "expired"
TAB_PAPER = "paper"
TAB_NO_TRADE = "no_trade"
TAB_OTHER = "other"

VALID_TABS = frozenset(
    {
        TAB_WAIT,
        TAB_READY,
        TAB_BLOCKED,
        TAB_INVALIDATED,
        TAB_EXPIRED,
        TAB_PAPER,
        TAB_NO_TRADE,
        TAB_OTHER,
    }
)

PAPER_PENDING = "pending"
PAPER_OPEN = "open"
PAPER_CLOSED = "closed"
VALID_PAPER_STATUS = frozenset({PAPER_PENDING, PAPER_OPEN, PAPER_CLOSED})

_TAB_SETUP_LIFECYCLES: dict[str, tuple[str, ...]] = {
    TAB_WAIT: (WAIT_LONG, WAIT_SHORT),
    TAB_READY: (READY_LONG, READY_SHORT),
    TAB_BLOCKED: (ICT_CONFIRMED_LONG, ICT_CONFIRMED_SHORT),
}

_COVERED_FOR_OTHER = frozenset(
    {
        WAIT_LONG,
        WAIT_SHORT,
        READY_LONG,
        READY_SHORT,
        ICT_CONFIRMED_LONG,
        ICT_CONFIRMED_SHORT,
        INVALIDATED,
        EXPIRED,
    }
)


def _paginate(total: int, page: int, page_size: int) -> dict[str, int]:
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = max(1, min(page, total_pages))
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "offset": (page - 1) * page_size,
    }


def _setup_order():
    return (desc(AiProposedSetup.updated_at), desc(AiProposedSetup.created_at), desc(AiProposedSetup.setup_id))


async def count_setups_for_tab(session: AsyncSession, tab: str) -> int:
    if tab == TAB_INVALIDATED:
        q = select(func.count()).select_from(AiProposedSetup).where(
            or_(AiProposedSetup.lifecycle_state == INVALIDATED, AiProposedSetup.status == "invalidated")
        )
    elif tab == TAB_EXPIRED:
        q = select(func.count()).select_from(AiProposedSetup).where(
            or_(AiProposedSetup.lifecycle_state == EXPIRED, AiProposedSetup.status == "expired")
        )
    elif tab in _TAB_SETUP_LIFECYCLES:
        q = (
            select(func.count())
            .select_from(AiProposedSetup)
            .where(
                AiProposedSetup.lifecycle_state.in_(_TAB_SETUP_LIFECYCLES[tab]),
                AiProposedSetup.status == "open",
            )
        )
    elif tab == TAB_OTHER:
        q = select(func.count()).select_from(AiProposedSetup).where(
            AiProposedSetup.status == "open",
            or_(
                AiProposedSetup.lifecycle_state.is_(None),
                AiProposedSetup.lifecycle_state.notin_(_COVERED_FOR_OTHER),
            ),
        )
    else:
        return 0
    return int((await session.execute(q)).scalar_one())


async def count_paper(session: AsyncSession, paper_status: str) -> int:
    statuses = _paper_db_statuses(paper_status)
    q = select(func.count()).select_from(SimPosition).where(SimPosition.status.in_(statuses))
    return int((await session.execute(q)).scalar_one())


def _paper_db_statuses(paper_status: str) -> tuple[str, ...]:
    if paper_status == PAPER_PENDING:
        return ("pending_entry",)
    if paper_status == PAPER_OPEN:
        return ("open",)
    return ("closed", "cancelled")


def _no_trade_clause():
    return and_(
        func.upper(AiAnalysisRun.decision) == "NO_TRADE",
        or_(AiAnalysisRun.final_ai_decision.is_(None), func.upper(AiAnalysisRun.final_ai_decision) != "WAIT"),
        or_(AiAnalysisRun.setup_mode.is_(None), AiAnalysisRun.setup_mode != "conditional"),
    )


async def count_no_trade(session: AsyncSession) -> int:
    q = select(func.count()).select_from(AiAnalysisRun).where(_no_trade_clause())
    return int((await session.execute(q)).scalar_one())


async def fetch_summary_counts(session: AsyncSession) -> dict[str, int]:
    return {
        "wait": await count_setups_for_tab(session, TAB_WAIT),
        "ready": await count_setups_for_tab(session, TAB_READY),
        "blocked": await count_setups_for_tab(session, TAB_BLOCKED),
        "expired": await count_setups_for_tab(session, TAB_EXPIRED),
        "invalidated": await count_setups_for_tab(session, TAB_INVALIDATED),
    }


async def list_tab_items(
    session: AsyncSession,
    *,
    tab: str,
    page: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    paper_status: str = PAPER_PENDING,
) -> tuple[list[Any], dict[str, int]]:
    page_size = min(20, max(1, page_size))
    if tab == TAB_PAPER:
        return await _list_paper(session, paper_status=paper_status, page=page, page_size=page_size)
    if tab == TAB_NO_TRADE:
        return await _list_no_trade(session, page=page, page_size=page_size)
    return await _list_setups(session, tab=tab, page=page, page_size=page_size)


async def _list_setups(
    session: AsyncSession,
    *,
    tab: str,
    page: int,
    page_size: int,
) -> tuple[list[AiProposedSetup], dict[str, int]]:
    if tab == TAB_INVALIDATED:
        base = select(AiProposedSetup).where(
            or_(AiProposedSetup.lifecycle_state == INVALIDATED, AiProposedSetup.status == "invalidated")
        )
    elif tab == TAB_EXPIRED:
        base = select(AiProposedSetup).where(
            or_(AiProposedSetup.lifecycle_state == EXPIRED, AiProposedSetup.status == "expired")
        )
    elif tab in _TAB_SETUP_LIFECYCLES:
        base = select(AiProposedSetup).where(
            AiProposedSetup.lifecycle_state.in_(_TAB_SETUP_LIFECYCLES[tab]),
            AiProposedSetup.status == "open",
        )
    elif tab == TAB_OTHER:
        base = select(AiProposedSetup).where(
            AiProposedSetup.status == "open",
            or_(
                AiProposedSetup.lifecycle_state.is_(None),
                AiProposedSetup.lifecycle_state.notin_(_COVERED_FOR_OTHER),
            ),
        )
    else:
        meta = _paginate(0, page, page_size)
        return [], meta

    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    meta = _paginate(total, page, page_size)
    rows = (
        await session.execute(base.order_by(*_setup_order()).offset(meta["offset"]).limit(page_size))
    ).scalars().all()
    return list(rows), meta


async def _list_paper(
    session: AsyncSession,
    *,
    paper_status: str,
    page: int,
    page_size: int,
) -> tuple[list[SimPosition], dict[str, int]]:
    statuses = _paper_db_statuses(paper_status)
    base = select(SimPosition).where(SimPosition.status.in_(statuses))
    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    meta = _paginate(total, page, page_size)
    rows = (
        await session.execute(
            base.order_by(desc(SimPosition.updated_at), desc(SimPosition.created_at)).offset(meta["offset"]).limit(
                page_size
            )
        )
    ).scalars().all()
    return list(rows), meta


async def _list_no_trade(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
) -> tuple[list[AiAnalysisRun], dict[str, int]]:
    base = select(AiAnalysisRun).where(_no_trade_clause())
    total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    meta = _paginate(total, page, page_size)
    rows = (
        await session.execute(base.order_by(desc(AiAnalysisRun.id)).offset(meta["offset"]).limit(page_size))
    ).scalars().all()
    return list(rows), meta
