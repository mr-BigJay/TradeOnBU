from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.execution_tp_cap import (
    EXECUTION_MAX_REWARD_R_MULTIPLE,
    apply_execution_take_profit_cap,
    gross_reward_r_multiple,
)
from app.models import SimPosition
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
