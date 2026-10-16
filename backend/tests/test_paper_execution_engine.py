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
from app.models import SimPosition, SimPositionSettings
from app.services.paper_entry_mode import choose_paper_entry_mode
from app.services.paper_execution_budget import (
    auto_paper_budget_allows_new_entry,
    count_auto_paper_entry_orders_tehran_today,
)
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
        await _wallet_and_settings(session, auto=True)
        now = datetime.now(timezone.utc)
        for i in range(MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY):
            session.add(
                SimPosition(
                    public_id=f"cap-{i}",
                    symbol="BTCUSDT",
                    direction="LONG",
                    status="closed",
                    order_type="limit",
                    margin_usdt=50,
                    leverage=10,
                    stop_loss=90,
                    take_profit=120,
                    created_at=now,
                    extra={"auto_paper": True},
                )
            )
        await session.commit()
        ok, reason, _ = await auto_paper_budget_allows_new_entry(session)
        assert ok is False
        assert reason == "daily_auto_paper_cap_tehran"
        assert await count_auto_paper_entry_orders_tehran_today(session) == MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY


@pytest.mark.asyncio
async def test_market_auto_open_when_live_near_entry():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup(lifecycle_state="READY_LONG")
        session.add(setup)
        await session.flush()
        validation = {"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True}
        structured = _ict_clear_structured()
        with patch(
            "app.services.conditional_revision.objective_confirmations_satisfied",
            return_value=(True, None),
        ), patch(
            "app.services.paper_auto_trading.SimPositionService.create_order",
            new_callable=AsyncMock,
        ) as mock_create:
            from app.services.position.execution_adapters import ExecutionResult

            mock_create.return_value = ExecutionResult(True, "pid-1", "ok", {"status": "open"})
            with patch(
                "app.services.paper_auto_trading.SimPositionService.get_position_by_public_id",
                new_callable=AsyncMock,
                return_value=SimPosition(
                    public_id="pid-1",
                    symbol="BTCUSDT",
                    direction="LONG",
                    status="open",
                    order_type="market",
                    margin_usdt=50,
                    leverage=10,
                    stop_loss=94,
                    take_profit=115,
                    extra={},
                ),
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
                )
        assert meta.get("order_type") == "market"
        req = mock_create.call_args[0][1]
        assert req.order_type == "market"
