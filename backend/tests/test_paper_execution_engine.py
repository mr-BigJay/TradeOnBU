from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

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
    reconcile_auto_paper_budget_from_db,
    release_auto_paper_active_slot,
    rollback_allocate_auto_paper_budget,
    try_allocate_auto_paper_budget,
)
from app.models import SimWalletLedger
from app.services.ai_setup_memory import create_setup_from_structured
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long
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
    await _reset_budget_counters()
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


async def _reset_budget_counters() -> None:
    async with SessionLocal() as session:
        state = await get_system_state(session)
        state.auto_paper_tehran_day = tehran_now().date().isoformat()
        state.auto_paper_orders_today = 0
        state.auto_paper_active_slots = 0
        await session.commit()


@pytest.mark.asyncio
async def test_allocate_rollback_restores_counters():
    await init_db()
    await _reset_budget_counters()
    async with SessionLocal() as session:
        ok, _, meta = await try_allocate_auto_paper_budget(session)
        assert ok
        assert meta["auto_paper_orders_tehran_today"] == 1
        await rollback_allocate_auto_paper_budget(session)
        async with SessionLocal() as read:
            state = await get_system_state(read)
            assert state.auto_paper_orders_today == 0
            assert state.auto_paper_active_slots == 0


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
            live_price=100.25,
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
            live_price=100.25,
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
        async with SessionLocal() as read:
            state2 = await get_system_state(read)
            assert state2.auto_paper_tehran_day == tehran_now().date().isoformat()
            assert state2.auto_paper_orders_today == 1


@pytest.mark.asyncio
async def test_market_order_rejected_when_execution_price_breaks_net_rr_no_ledger():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup(lifecycle_state="READY_LONG", take_profit=101.2)
        session.add(setup)
        await session.flush()

        class _M:
            async def fetch_ticker(self, _symbol: str):
                return 101.0, 1

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
                commit=False,
                require_ai_flags=False,
                live_price=100.02,
                market=_M(),  # type: ignore[arg-type]
            )
        await session.rollback()
        assert meta.get("created") is not True
        assert meta.get("reason") in ("net_rr_below_min_at_execution_price", "net_rr_below_min")
        async with SessionLocal() as check:
            pos_n = (await check.execute(select(func.count()).select_from(SimPosition))).scalar_one()
            ledger_n = (await check.execute(select(func.count()).select_from(SimWalletLedger))).scalar_one()
            assert pos_n == 0
            assert ledger_n == 1


@pytest.mark.asyncio
async def test_create_order_market_min_net_rr_fail_before_margin_reserve():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()

        class _M:
            async def fetch_ticker(self, _symbol: str):
                return 101.0, 1

        from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
        from app.services.position.execution_adapters import OrderRequest
        from app.services.position.position_service import PositionServiceError, SimPositionService

        svc = SimPositionService(market=_M())  # type: ignore[arg-type]
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=settings.default_margin_usdt,
            leverage=settings.default_leverage,
            order_type="market",
            entry_price=None,
            stop_loss=94.0,
            take_profit=101.15,
            min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
        )
        with pytest.raises(PositionServiceError) as exc:
            await svc.create_order(session, req, commit=False)
        assert "net_rr_below_min" in str(exc.value)
        await session.rollback()
        pos_n = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert pos_n == 0


@pytest.mark.asyncio
async def test_concurrent_isolated_budget_only_one_active_slot():
    await init_db()

    async def _alloc():
        return await try_allocate_auto_paper_budget()

    results = await asyncio.gather(_alloc(), _alloc(), _alloc())
    oks = sum(1 for ok, _, _ in results if ok)
    assert oks == 1
    for ok, reason, _ in results:
        if not ok:
            assert reason == "max_concurrent_auto_paper"
    await rollback_allocate_auto_paper_budget()


@pytest.mark.asyncio
async def test_reconcile_budget_from_db_on_day_rollover():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup()
        session.add(setup)
        session.add(
            SimPosition(
                public_id="reconcile-pos",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50,
                leverage=10,
                stop_loss=94,
                take_profit=115,
                extra={"auto_paper": True},
            )
        )
        await session.commit()
        state = await get_system_state(session)
        state.auto_paper_tehran_day = (tehran_now().date() - timedelta(days=1)).isoformat()
        state.auto_paper_orders_today = 99
        state.auto_paper_active_slots = 99
        await session.commit()
        await reconcile_auto_paper_budget_from_db(session)
        await session.commit()
        state = await get_system_state(session)
        assert state.auto_paper_tehran_day == tehran_now().date().isoformat()
        assert state.auto_paper_orders_today == 1
        assert state.auto_paper_active_slots == 1


@pytest.mark.asyncio
async def test_conditional_wait_hourly_dedupe_single_open_row():
    await init_db()
    async with SessionLocal() as session:
        structured = _production_conditional_wait_long()
        row1, meta1 = await create_setup_from_structured(
            session, run_id=1, symbol="BTCUSDT", structured=structured
        )
        assert row1 is not None
        assert meta1.get("reason") in ("conditional_created", "conditional_revision")
        sid = row1.setup_id
        row2, meta2 = await create_setup_from_structured(
            session, run_id=2, symbol="BTCUSDT", structured=structured
        )
        assert row2 is not None
        assert row2.setup_id == sid
        assert meta2.get("reason") in ("conditional_revision", "deduped_revision_appended", "conditional_created")
        open_count = (
            await session.execute(
                select(func.count()).select_from(AiProposedSetup).where(AiProposedSetup.status == "open")
            )
        ).scalar_one()
        assert open_count == 1
