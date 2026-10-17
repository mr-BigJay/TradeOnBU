from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.paper_execution_policy import LIMIT_ENTRY_MAX_DISTANCE_BPS
from app.domain.setup_lifecycle import PENDING_ENTRY, READY_LONG
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.paper_auto_trading import maybe_auto_open_paper_position
from app.services.paper_execution_budget import try_allocate_auto_paper_budget
from app.services.paper_entry_mode import choose_paper_entry_mode
from app.services.system_state import get_system_state
from tests.helpers.paper_live import fresh_ticker
from tests.helpers.ready_setup import ready_long_setup
from tests.test_paper_auto_trading import _ict_clear_structured, _wallet_and_settings


async def _reset_budget() -> None:
    from app.services.paper_execution_budget import reconcile_auto_paper_budget_from_db

    async with SessionLocal() as session:
        await reconcile_auto_paper_budget_from_db(session)
        state = await get_system_state(session)
        state.auto_paper_orders_today = 0
        state.auto_paper_active_slots = 0
        await session.commit()


@pytest.mark.asyncio
async def test_skip_entry_does_not_touch_budget_counters():
    """Regression: skip_entry must not call rollback before allocation."""
    await init_db()
    await _reset_budget()
    far = 100.0 * (1 + (LIMIT_ENTRY_MAX_DISTANCE_BPS + 5) / 10_000)
    price, ts = fresh_ticker(far)
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = ready_long_setup()
        session.add(setup)
        await session.flush()
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True},
            structured=_ict_clear_structured(),
            symbol="BTCUSDT",
            live_price=price,
            ticker_time_ms=ts,
        )
        assert meta["skipped"] is True
        assert meta["reason"] == "entry_too_far_from_live"
        state = await get_system_state(session)
        assert state.auto_paper_orders_today == 0
        assert state.auto_paper_active_slots == 0


@pytest.mark.asyncio
async def test_live_price_required_fail_closed():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = ready_long_setup()
        session.add(setup)
        await session.flush()
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True},
            structured=_ict_clear_structured(),
            symbol="BTCUSDT",
            live_price=None,
            ticker_time_ms=None,
        )
        assert meta["reason"] == "live_price_unavailable"


@pytest.mark.asyncio
async def test_concurrent_budget_cap_two_orders():
    from app.services.paper_execution_budget import release_auto_paper_active_slot

    await init_db()
    await _reset_budget()

    async def one_allocate():
        async with SessionLocal() as session:
            ok, reason, _ = await try_allocate_auto_paper_budget(session)
            if ok:
                await release_auto_paper_active_slot(session)
            await session.commit()
            return ok, reason

    results = await asyncio.gather(one_allocate(), one_allocate(), one_allocate())
    assert sum(1 for ok, _ in results if ok) == 2
    assert sum(1 for ok, r in results if not ok and r == "daily_auto_paper_cap_tehran") == 1


@pytest.mark.asyncio
async def test_best_ready_setup_wins_by_rank():
    await init_db()
    from app.services.opportunity_monitor import _process_ready_paper_queue

    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        now = datetime.now(timezone.utc)
        low = ready_long_setup(setup_id="ready-low", take_profit=110.0)
        high = ready_long_setup(setup_id="ready-high", take_profit=120.0)
        session.add_all([low, high])
        await session.flush()
        price, ts = fresh_ticker(100.25)
        summary: dict = {"orders": 0}
        await _process_ready_paper_queue(
            session,
            ["ready-low", "ready-high"],
            now=now,
            summary=summary,
            live_price=price,
            ticker_time_ms=ts,
            market=None,
        )
        assert summary["ready_selection"]["setup_id"] == "ready-high"
        count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert count == 1
        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert pos.ai_setup_id == "ready-high"


@pytest.mark.asyncio
async def test_win_calibration_insufficient_closed_samples_status():
    from app.services.paper_opportunity_calibration import win_calibration_for_bucket

    await init_db()
    async with SessionLocal() as session:
        cal = await win_calibration_for_bucket(
            session, direction="LONG", order_type="limit", distance_bps=30.0
        )
        assert cal.status in ("uncalibrated", "insufficient_closed_samples")
