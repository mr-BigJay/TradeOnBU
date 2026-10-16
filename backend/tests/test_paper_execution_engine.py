from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.paper_execution_policy import (
    LIMIT_ENTRY_MAX_DISTANCE_BPS,
    MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY,
)
from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN
from app.domain.tehran_time import tehran_midnight_to_utc, tehran_now
from app.models import AiProposedSetup, SimPosition, SimPositionSettings, SystemState
from app.services.paper_entry_mode import choose_paper_entry_mode
from app.services.paper_entry_policy_compare import compare_entry_policies_on_history
from app.services.paper_execution_budget import (
    release_auto_paper_active_slot,
    rollback_allocate_auto_paper_budget,
    try_allocate_auto_paper_budget,
)
from app.services.system_state import get_system_state
from app.services.paper_auto_trading import maybe_auto_open_paper_position
from app.services.position import wallet_service as wallet
from tests.test_paper_auto_trading import _ict_clear_structured, _sample_setup, _wallet_and_settings


def test_choose_market_when_long_live_at_entry():
    ot, skip, diag = choose_paper_entry_mode(direction="LONG", entry=100.0, live_price=100.05)
    assert skip is None
    assert ot == "market"
    assert diag["mode_reason"] == "confirmed_near_or_favorable_market"


def test_choose_limit_when_live_far_above_long_entry():
    far = 100.0 * (1 + (LIMIT_ENTRY_MAX_DISTANCE_BPS + 10) / 10_000)
    ot, skip, _ = choose_paper_entry_mode(direction="LONG", entry=100.0, live_price=far)
    assert skip == "entry_too_far_from_live"
    assert ot == "limit"


def test_choose_limit_retest_when_live_above_entry():
    ot, skip, diag = choose_paper_entry_mode(direction="LONG", entry=100.0, live_price=100.5)
    assert skip is None
    assert ot == "limit"
    assert diag["mode_reason"] == "poi_retest_limit"


@pytest.mark.asyncio
async def test_daily_cap_blocks_third_auto_order():
    await init_db()
    async with SessionLocal() as session:
        ok, reason, _ = await try_allocate_auto_paper_budget(session)
        assert ok, reason
        await release_auto_paper_active_slot(session)
        ok, reason, _ = await try_allocate_auto_paper_budget(session)
        assert ok, reason
        await release_auto_paper_active_slot(session)
        ok, reason, _ = await try_allocate_auto_paper_budget(session)
        assert ok is False
        assert reason == "daily_auto_paper_cap_tehran"


@pytest.mark.asyncio
async def test_allocate_rollback_restores_counters():
    await init_db()
    async with SessionLocal() as session:
        ok, _, meta = await try_allocate_auto_paper_budget(session)
        assert ok
        before = meta["auto_paper_orders_tehran_today"]
        await rollback_allocate_auto_paper_budget(session)
        state = (await session.execute(select(SystemState))).scalar_one()
        assert state.auto_paper_orders_today == before - 1


@pytest.mark.asyncio
async def test_market_auto_open_sets_position_open_and_paper_fill():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup(lifecycle_state="READY_LONG")
        session.add(setup)
        await session.flush()

        class _M:
            async def fetch_ticker(self, _symbol: str):
                return 100.02, 1

        validation = {"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True}
        structured = _ict_clear_structured()
        with patch(
            "app.services.conditional_revision.objective_confirmations_satisfied",
            return_value=(True, None),
        ):
            meta = await maybe_auto_open_paper_position(
                session,
                run_id=1,
                setup=setup,
                validation=validation,
                structured=structured,
                symbol="BTCUSDT",
                commit=True,
                require_ai_flags=False,
                live_price=100.02,
                market=_M(),  # type: ignore[arg-type]
            )
        assert meta.get("created") is True
        assert meta.get("order_type") == "market"
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup.setup_id))
        ).scalar_one()
        assert pos.status == "open"
        assert pos.extra.get("auto_paper") is True
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup.setup_id))
        ).scalar_one()
        assert setup.lifecycle_state == POSITION_OPEN
        assert setup.paper_fill_recorded is True


@pytest.mark.asyncio
async def test_duplicate_order_blocked_for_same_setup():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup()
        session.add(setup)
        await session.flush()
        validation = {"ai_decision": "LONG", "acceptable_rr": True}
        structured = _ict_clear_structured()
        m = AsyncMock()
        m.fetch_ticker = AsyncMock(return_value=(100.0, 1))
        meta1 = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation=validation,
            structured=structured,
            symbol="BTCUSDT",
            commit=False,
            live_price=101.0,
            market=m,
        )
        assert meta1["created"]
        meta2 = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation=validation,
            structured=structured,
            symbol="BTCUSDT",
            commit=False,
            live_price=101.0,
            market=m,
        )
        assert meta2["skipped"]
        assert meta2["reason"] == "position_already_active_for_setup"


@pytest.mark.asyncio
async def test_entry_policy_compare_uses_promotion_snapshot_only():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="snap-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="READY_LONG",
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            last_validation_result={
                "ok": True,
                "promoted_at": "2026-10-10T12:00:00+00:00",
                "live_price_at_ready": 100.1,
            },
        )
        session.add(setup)
        await session.commit()
        report = await compare_entry_policies_on_history(session, symbol="BTCUSDT", limit=10)
        assert report["report_type"] == "entry_policy_compare"
        assert report["not_a_profit_backtest"] is True
        row = report["details"][0]
        assert row["live_price_at_ready"] == 100.1
        assert row["promoted_at"] == "2026-10-10T12:00:00+00:00"


@pytest.mark.asyncio
async def test_tehran_midnight_resets_day_counter():
    await init_db()
    async with SessionLocal() as session:
        state = await get_system_state(session)
        yesterday = (tehran_now().date() - timedelta(days=1)).isoformat()
        state.auto_paper_tehran_day = yesterday
        state.auto_paper_orders_today = 2
        state.auto_paper_active_slots = 0
        await session.commit()
        ok, reason, meta = await try_allocate_auto_paper_budget(session)
        assert ok, reason
        assert meta["auto_paper_orders_tehran_today"] == 1
