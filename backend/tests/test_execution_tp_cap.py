from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.execution_tp_cap import (
    EXECUTION_MAX_REWARD_R_MULTIPLE,
    apply_execution_take_profit_cap,
    gross_reward_r_multiple,
)
from app.models import SimPosition, SimPositionSettings
from app.services.position.execution_adapters import OrderRequest
from app.services.position.position_service import SimPositionService
from app.services.position import wallet_service as wallet
from tests.helpers.market_fakes import EmptyCompleteMarket


def test_cap_module_long_example_from_spec():
    cap = apply_execution_take_profit_cap(
        direction="LONG",
        entry=80_000.0,
        stop_loss=79_500.0,
        take_profit_ai=82_000.0,
    )
    assert cap.execution_take_profit == 81_500.0
    assert cap.was_capped is True
    assert gross_reward_r_multiple("LONG", 80_000.0, 79_500.0, cap.execution_take_profit) == pytest.approx(3.0)
    assert gross_reward_r_multiple("LONG", 80_000.0, 79_500.0, cap.ai_take_profit) == pytest.approx(4.0)


def test_cap_unchanged_at_3r_and_below():
    cap = apply_execution_take_profit_cap(
        direction="LONG",
        entry=100.0,
        stop_loss=95.0,
        take_profit_ai=115.0,
    )
    assert cap.execution_take_profit == 115.0
    assert cap.was_capped is False


def test_cap_short_unchanged_under_3r():
    cap = apply_execution_take_profit_cap(
        direction="SHORT",
        entry=100.0,
        stop_loss=105.0,
        take_profit_ai=90.0,
    )
    assert cap.execution_take_profit == 90.0
    assert cap.was_capped is False


def test_cap_short_above_3r():
    cap = apply_execution_take_profit_cap(
        direction="SHORT",
        entry=100.0,
        stop_loss=105.0,
        take_profit_ai=80.0,
    )
    assert cap.execution_take_profit == 85.0
    assert cap.was_capped is True


def test_cap_tp2_both_limited():
    cap = apply_execution_take_profit_cap(
        direction="LONG",
        entry=100.0,
        stop_loss=90.0,
        take_profit_ai=140.0,
        take_profit_2_ai=150.0,
    )
    assert cap.execution_take_profit == 130.0
    assert cap.execution_take_profit_2 == 130.0
    assert cap.was_capped and cap.was_capped_tp2


@pytest.mark.asyncio
async def test_create_order_applies_cap_once_and_preserves_ai_tp_in_extra():
    await init_db()
    market = EmptyCompleteMarket(mark_price=80_000.0)
    svc = SimPositionService(market=market)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        await session.commit()
    async with SessionLocal() as session:
        result = await svc.create_order(
            session,
            OrderRequest(
                symbol="BTCUSDT",
                direction="LONG",
                margin_usdt=100.0,
                leverage=10,
                order_type="limit",
                entry_price=80_000.0,
                stop_loss=79_500.0,
                take_profit=82_000.0,
            ),
        )
        assert result.success
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == result.position_id))
        ).scalar_one()
        assert pos.take_profit == 81_500.0
        assert pos.extra["ai_original_take_profit"] == 82_000.0
        assert pos.extra["execution_tp_cap_applied_at"]
        assert pos.extra["execution_tp_cap_capped"] is True


@pytest.mark.asyncio
async def test_market_fill_recaps_tp_for_short_after_slippage():
    await init_db()
    market = EmptyCompleteMarket(mark_price=100.0)
    svc = SimPositionService(market=market)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if settings is None:
            settings = SimPositionSettings(id=1, slippage_bps=10.0)
            session.add(settings)
        else:
            settings.slippage_bps = 10.0
        await session.commit()
    async with SessionLocal() as session:
        result = await svc.create_order(
            session,
            OrderRequest(
                symbol="BTCUSDT",
                direction="SHORT",
                margin_usdt=100.0,
                leverage=10,
                order_type="market",
                entry_price=None,
                stop_loss=105.0,
                take_profit=80.0,
            ),
        )
        assert result.success
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == result.position_id))
        ).scalar_one()
        assert pos.status == "open"
        assert pos.extra.get("execution_tp_cap_post_fill_applied") is True
        assert pos.take_profit <= 85.0
        rr = gross_reward_r_multiple("SHORT", float(pos.fill_price), 105.0, pos.take_profit)
        assert rr <= EXECUTION_MAX_REWARD_R_MULTIPLE + 1e-6


@pytest.mark.asyncio
async def test_auto_paper_to_sim_position_caps_34_rr_integration():
    from unittest.mock import patch

    from app.services.paper_auto_trading import maybe_auto_open_paper_position
    from tests.test_paper_auto_trading import _ict_clear_structured, _sample_setup, _wallet_and_settings

    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup(
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=117.0,
            lifecycle_state="READY_LONG",
        )
        setup.structured_snapshot = {
            **_ict_clear_structured(),
            "take_profit_2": 120.0,
        }
        session.add(setup)
        await session.flush()
        validation = {"ai_decision": "LONG", "acceptable_rr": True}
        structured = dict(setup.structured_snapshot or {})
        with patch(
            "app.services.conditional_revision.objective_confirmations_satisfied",
            return_value=(True, None),
        ):
            meta = await maybe_auto_open_paper_position(
                session,
                run_id=77,
                setup=setup,
                validation=validation,
                structured=structured,
                symbol="BTCUSDT",
                commit=True,
                require_ai_flags=False,
            )
        assert meta["created"] is True
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup.setup_id))
        ).scalar_one()
        assert pos.stop_loss == 95.0
        assert pos.limit_entry_price == 100.0
        assert pos.extra["ai_original_take_profit"] == 117.0
        assert pos.take_profit == 115.0
        assert pos.extra["ai_original_take_profit_2"] == 120.0
        assert pos.extra.get("execution_take_profit_2_audit_only") is True
        assert gross_reward_r_multiple("LONG", 100.0, 95.0, pos.take_profit) == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_market_long_post_fill_updates_execution_tp2_audit():
    await init_db()
    market = EmptyCompleteMarket(mark_price=100.0)
    svc = SimPositionService(market=market)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if settings is None:
            settings = SimPositionSettings(id=1, slippage_bps=5.0)
            session.add(settings)
        else:
            settings.slippage_bps = 5.0
        await session.commit()
    async with SessionLocal() as session:
        result = await svc.create_order(
            session,
            OrderRequest(
                symbol="BTCUSDT",
                direction="LONG",
                margin_usdt=100.0,
                leverage=10,
                order_type="market",
                entry_price=None,
                stop_loss=95.0,
                take_profit=117.0,
                take_profit_2=120.0,
            ),
        )
        assert result.success
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == result.position_id))
        ).scalar_one()
        assert pos.extra.get("execution_tp_cap_post_fill_applied") is True
        assert pos.extra["ai_original_take_profit_2"] == 120.0
        assert pos.extra["execution_take_profit_2"] is not None
        rr = gross_reward_r_multiple("LONG", float(pos.fill_price), 95.0, pos.take_profit)
        assert rr <= EXECUTION_MAX_REWARD_R_MULTIPLE + 1e-6


@pytest.mark.asyncio
async def test_post_fill_cap_idempotent_second_apply():
    from app.services.execution_tp_cap_apply import compute_post_market_fill_execution_tp

    await init_db()
    async with SessionLocal() as session:
        pos = SimPosition(
            public_id="idem-1",
            symbol="BTCUSDT",
            direction="LONG",
            status="pending_entry",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            stop_loss=95.0,
            take_profit=115.0,
            extra={
                "execution_tp_cap_applied_at": "2026-01-01T00:00:00+00:00",
                "ai_original_take_profit": 117.0,
                "ai_original_take_profit_2": 120.0,
            },
        )
        session.add(pos)
        await session.flush()
        r1 = compute_post_market_fill_execution_tp(pos, fill_price=100.0)
        pos.take_profit = r1.take_profit
        pos.extra = r1.extra
        r2 = compute_post_market_fill_execution_tp(pos, fill_price=100.5)
        assert r2.applied is False
        assert r2.take_profit == r1.take_profit


@pytest.mark.asyncio
async def test_invalid_fill_geometry_cancels_pending_without_open_fee():
    from app.services.position.settings_service import get_or_create_settings

    await init_db()
    market = EmptyCompleteMarket(mark_price=100.0)
    svc = SimPositionService(market=market)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = await get_or_create_settings(session)
        settings.slippage_bps = 0.0
        await session.commit()
    async with SessionLocal() as session:
        settings = await get_or_create_settings(session)
        pos = SimPosition(
            public_id="geom-bad",
            symbol="BTCUSDT",
            direction="LONG",
            status="pending_entry",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            stop_loss=98.0,
            take_profit=101.0,
            extra={
                "execution_tp_cap_applied_at": "2026-01-01T00:00:00+00:00",
                "ai_original_take_profit": 101.0,
            },
        )
        session.add(pos)
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_MARGIN_RESERVE,
            amount_usdt=50.0,
            reference_type="sim_position",
            reference_id="geom-bad",
            affects_balance=False,
        )
        await session.flush()
        ok = await svc._fill_entry(session, pos, settings, 102.0, transition_suffix="test")
        await session.commit()
        assert ok is False
        assert pos.status == "cancelled"
        assert pos.fill_price is None
        assert pos.entry_fee_usdt == 0.0


@pytest.mark.asyncio
async def test_existing_position_row_unchanged_by_cap_logic():
    """Positions without cap metadata keep stored TP (simulates pre-feature rows)."""
    await init_db()
    async with SessionLocal() as session:
        pos = SimPosition(
            public_id="legacy-1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            entry_price=100.0,
            fill_price=100.0,
            quantity_btc=0.01,
            stop_loss=90.0,
            take_profit=150.0,
            extra={},
        )
        session.add(pos)
        await session.commit()
        tp_before = pos.take_profit
    async with SessionLocal() as session:
        row = (await session.execute(select(SimPosition).where(SimPosition.public_id == "legacy-1"))).scalar_one()
        assert row.take_profit == tp_before == 150.0
        assert row.extra.get("ai_original_take_profit") is None
