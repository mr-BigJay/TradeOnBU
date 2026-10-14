from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.tehran_time import format_tehran
from app.models import SimPosition, SimWalletLedger
from app.services.position.calculations import (
    compute_metrics,
    fee_on_notional,
    gross_pnl,
    position_notional,
    quantity_from_notional,
)
from app.services.position.execution_adapters import BitunixLiveExecutionAdapter, ExecutionBackend
from app.services.position.fee_config import BITUNIX_VIP1_MAKER_RATE, BITUNIX_VIP1_TAKER_RATE, default_vip1_profile
from app.services.position.position_monitor import _tp_sl_on_bar
from app.services.position.position_service import SimPositionService
from app.services.position import wallet_service as wallet
from app.services.position.execution_adapters import OrderRequest
from app.domain.candles import Candle


@pytest.mark.asyncio
async def test_wallet_deposit_withdraw():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_DEPOSIT, amount_usdt=100.0)
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_WITHDRAW, amount_usdt=-50.0)
        await db_session.commit()
        summary = await wallet.wallet_summary(db_session)
        assert summary["wallet_balance"] == 1050.0
        assert summary["available_balance"] == 1050.0


@pytest.mark.asyncio
async def test_margin_available_reservation():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=100.0)
        await wallet.append_ledger(
            db_session,
            entry_type=wallet.LEDGER_MARGIN_RESERVE,
            amount_usdt=40.0,
            affects_balance=False,
        )
        db_session.add(
            SimPosition(
                public_id="p1",
                direction="LONG",
                status="open",
                margin_usdt=40.0,
                leverage=10,
                stop_loss=90.0,
                take_profit=110.0,
            )
        )
        await db_session.commit()
        summary = await wallet.wallet_summary(db_session)
        assert summary["wallet_balance"] == 100.0
        assert summary["margin_in_use"] == 40.0
        assert summary["available_balance"] == 60.0


def test_vip1_fee_rates():
    profile = default_vip1_profile()
    assert profile.maker_rate == BITUNIX_VIP1_MAKER_RATE == 0.0002
    assert profile.taker_rate == BITUNIX_VIP1_TAKER_RATE == 0.0005


def test_fee_on_notional():
    notional = position_notional(50, 10)
    fee = fee_on_notional(notional, 100_000.0, 0.0005)
    assert fee == pytest.approx(notional * 0.0005, rel=1e-6)


def test_long_short_pnl():
    qty = quantity_from_notional(500, 100.0)
    assert gross_pnl("LONG", 100.0, 105.0, qty) > 0
    assert gross_pnl("SHORT", 100.0, 95.0, qty) > 0


def test_tehran_timezone_format():
    dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    s = format_tehran(dt)
    assert s is not None
    assert "2026" in s


def test_ambiguous_intrabar():
    bar = Candle(time_ms=1, open=100.0, high=110.0, low=90.0, close=100.0, volume=1.0)
    reason, price, amb = _tp_sl_on_bar("LONG", bar, sl=95.0, tp=105.0)
    assert amb is True
    assert reason is None


@pytest.mark.asyncio
async def test_manual_close_and_fees():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=1.0,
            take_profit=1_000_000.0,
        )
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.create_order(db_session, req)
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        assert pos.status == "open"
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(101.0, None))):
            await svc.manual_close(db_session, pos.public_id)
        await db_session.refresh(pos)
        assert pos.status == "closed"
        assert pos.net_pnl_usdt is not None
        fees = await wallet.sum_fees(db_session)
        assert fees["opening_fees"] > 0
        assert fees["closing_fees"] > 0


@pytest.mark.asyncio
async def test_restart_idempotent_close():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=1.0,
            take_profit=1_000_000.0,
        )
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.create_order(db_session, req)
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(101.0, None))):
            await svc.manual_close(db_session, pos.public_id)
            await svc.manual_close(db_session, pos.public_id)
        ledgers = (
            await db_session.execute(
                select(SimWalletLedger).where(SimWalletLedger.entry_type == wallet.LEDGER_REALIZED_PNL)
            )
        ).scalars().all()
        assert len(ledgers) == 1


@pytest.mark.asyncio
async def test_live_adapter_disabled():
    live = BitunixLiveExecutionAdapter()
    res = await live.submit_order(
        OrderRequest("BTCUSDT", "LONG", 10, 5, "market", None, 1, 2)
    )
    assert res.success is False
    assert ExecutionBackend.BITUNIX_LIVE.value == "bitunix_live"


def test_leverage_metrics():
    m = compute_metrics(
        direction="LONG",
        margin_usdt=50,
        leverage=10,
        entry_price=100,
        mark_price=101,
        stop_loss=98,
        take_profit=106,
        fees=default_vip1_profile(),
        entry_is_maker=False,
        exit_is_maker=False,
    )
    assert m.notional_usdt == 500
    assert m.quantity_btc == pytest.approx(5.0)
