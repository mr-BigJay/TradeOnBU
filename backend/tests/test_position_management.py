from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.position_form_merge import merge_open_form_defaults
from app.domain.tehran_time import format_tehran, report_period_bounds_utc, tehran_midnight_to_utc, tehran_now
from app.services.ai_telegram_format import format_ai_chart_caption
from app.services.position.mark_to_market import aggregate_open_mark_to_market
from app.services.position.position_service import PositionServiceError
from app.models import SimEquitySnapshot, SimPosition, SimPositionSettings, SimWalletLedger
from app.services.position.calculations import (
    apply_slippage,
    compute_metrics,
    fee_on_notional,
    fee_on_quantity,
    gross_pnl,
    position_notional,
    quantity_from_notional,
)
from app.services.position.position_monitor import SimPositionMonitorService
from app.services.position.report_service import build_report
from app.services.position.settings_service import SettingsValidationError, validate_settings_patch
from app.services.market_data import KlineBundle
from app.services.position.execution_adapters import BitunixLiveExecutionAdapter, ExecutionBackend
from app.services.position.fee_config import BITUNIX_VIP1_MAKER_RATE, BITUNIX_VIP1_TAKER_RATE, default_vip1_profile
from app.services.position.position_monitor import _tp_sl_on_bar
from app.services.position.position_service import SimPositionService
from app.services.position import wallet_service as wallet
from app.services.position.execution_adapters import OrderRequest
from app.domain.candles import Candle


async def _ensure_paper_settings(db_session, **kwargs) -> SimPositionSettings:
    row = (await db_session.execute(select(SimPositionSettings))).scalar_one_or_none()
    if not row:
        row = SimPositionSettings(id=1, **kwargs)
        db_session.add(row)
    else:
        for k, v in kwargs.items():
            setattr(row, k, v)
    await db_session.commit()
    return row


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
        await _ensure_paper_settings(db_session, max_risk_pct_per_position=100.0)
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=99.0,
            take_profit=110.0,
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
        await _ensure_paper_settings(db_session, max_risk_pct_per_position=100.0)
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=99.0,
            take_profit=110.0,
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


def test_exit_slippage_direction():
    raw = 100.0
    long_exit = apply_slippage(raw, "LONG", side="exit", adverse=True, bps=10)
    short_exit = apply_slippage(raw, "SHORT", side="exit", adverse=True, bps=10)
    assert long_exit < raw
    assert short_exit > raw


def test_fee_on_quantity_uses_fixed_btc():
    qty = 5.0
    assert fee_on_quantity(qty, 100.0, 0.001) == pytest.approx(0.5)
    assert fee_on_quantity(qty, 110.0, 0.001) == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_no_double_fee_deduction_zero_gross_trade():
    """Initial 1000, gross 0, open/close fee 0.50 each → wallet 999, net -1."""
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(
            db_session,
            taker_fee_rate=0.001,
            maker_fee_rate=0.001,
            slippage_bps=0.0,
            max_margin_per_position=500.0,
            max_risk_pct_per_position=100.0,
        )

        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=99.0,
            take_profit=110.0,
        )
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.create_order(db_session, req)
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        assert pos.entry_fee_usdt == pytest.approx(0.5, abs=1e-6)
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.manual_close(db_session, pos.public_id)
        await db_session.refresh(pos)
        assert pos.gross_pnl_usdt == pytest.approx(0.0, abs=1e-6)
        assert pos.net_pnl_usdt == pytest.approx(-1.0, abs=1e-6)
        summary = await wallet.wallet_summary(db_session)
        assert summary["wallet_balance"] == pytest.approx(999.0, abs=1e-4)
        assert summary["realized_pnl"] == pytest.approx(-1.0, abs=1e-4)


@pytest.mark.asyncio
async def test_ambiguous_intrabar_not_recorded_as_sl():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        svc = SimPositionService()
        pos = SimPosition(
            public_id="amb1",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.1,
            stop_loss=95.0,
            take_profit=105.0,
            opened_at=datetime.now(timezone.utc),
        )
        db_session.add(pos)
        await db_session.commit()
        settings = (await db_session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            db_session.add(SimPositionSettings(id=1, slippage_bps=0))
            await db_session.commit()
            settings = (await db_session.execute(select(SimPositionSettings))).scalar_one()
        await svc._close_position(
            db_session,
            pos,
            settings,
            raw_exit=100.0,
            reason="ambiguous_intrabar",
            exit_adverse=True,
            bar_ms=123,
            ambiguous=True,
        )
        await db_session.commit()
        await db_session.refresh(pos)
        assert pos.close_reason == "ambiguous_intrabar"
        assert pos.status == "closed"
        assert pos.extra.get("settlement_policy") == "bar_close"


@pytest.mark.asyncio
async def test_max_risk_pct_enforced():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=100.0)
        db_session.add(
            SimPositionSettings(
                id=1,
                max_risk_pct_per_position=1.0,
                max_margin_per_position=500.0,
                slippage_bps=0,
            )
        )
        await db_session.commit()
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="limit",
            entry_price=100.0,
            stop_loss=50.0,
            take_profit=200.0,
        )
        with pytest.raises(PositionServiceError):
            await svc.create_order(db_session, req)


def test_settings_validation_rejects_bad_values():
    with pytest.raises(SettingsValidationError):
        validate_settings_patch({"maker_fee_rate": -0.001})
    with pytest.raises(SettingsValidationError):
        validate_settings_patch({"default_leverage": 200})
    with pytest.raises(SettingsValidationError):
        validate_settings_patch({"default_order_type": "stop"})


def test_tehran_today_filter_uses_tehran_midnight():
    start, _end = report_period_bounds_utc("today")
    assert start == tehran_midnight_to_utc(tehran_now().date())


@pytest.mark.asyncio
async def test_report_gross_profit_factor_independent_of_net_wins():
    await init_db()
    async with SessionLocal() as db_session:
        db_session.add(
            SimPosition(
                public_id="r1",
                direction="LONG",
                status="closed",
                margin_usdt=10,
                leverage=5,
                fill_price=100,
                stop_loss=90,
                take_profit=120,
                gross_pnl_usdt=20.0,
                net_pnl_usdt=-1.0,
                closed_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()
        report = await build_report(db_session, period="all")
        assert report["gross_profits"] == pytest.approx(20.0)
        assert report["winning_trades"] == 0


@pytest.mark.asyncio
async def test_equity_snapshot_on_close():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        db_session.add(SimPositionSettings(id=1, slippage_bps=0, max_risk_pct_per_position=100))
        await db_session.commit()
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="SHORT",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=200.0,
            take_profit=1.0,
        )
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.create_order(db_session, req)
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.manual_close(db_session, pos.public_id)
        snaps = (await db_session.execute(select(SimEquitySnapshot))).scalars().all()
        assert len(snaps) >= 1


@pytest.mark.asyncio
async def test_monitor_tp_hit_long():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        db_session.add(SimPositionSettings(id=1, slippage_bps=0, max_risk_pct_per_position=100))
        pos = SimPosition(
            public_id="tp1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.25,
            stop_loss=90.0,
            take_profit=105.0,
            monitor_from_ms=0,
            last_processed_candle_ms=0,
            opened_at=datetime.now(timezone.utc),
        )
        db_session.add(pos)
        await db_session.commit()

        bar = Candle(time_ms=60_000, open=100, high=106, low=99, close=104, volume=1.0)
        bundle = KlineBundle(
            closed=[bar], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m"
        )

        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            n = await mon.monitor_session(db_session)
        assert n >= 1
        await db_session.refresh(pos)
        assert pos.status == "tp_hit"


@pytest.mark.asyncio
async def test_monitor_recovery_records_gap_when_history_missing():
    await init_db()
    async with SessionLocal() as db_session:
        db_session.add(
            SimPosition(
                public_id="gap1",
                symbol="BTCUSDT",
                direction="LONG",
                status="open",
                order_type="market",
                margin_usdt=10,
                leverage=2,
                fill_price=100,
                stop_loss=90,
                take_profit=110,
                monitor_from_ms=0,
                last_processed_candle_ms=1_000_000_000_000,
            )
        )
        await db_session.commit()
        recent = Candle(time_ms=1_700_000_000_000, open=100, high=101, low=99, close=100, volume=1)
        mon = SimPositionMonitorService()
        gap_bundle = KlineBundle(
            closed=[recent], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=False, interval="1m"
        )
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=gap_bundle)):
            await mon.monitor_session(db_session)
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        assert pos.extra.get("monitor_gaps")
        assert pos.execution_state == "suspended"
        assert pos.last_processed_candle_ms == 1_000_000_000_000


@pytest.mark.asyncio
async def test_contiguous_next_minute_stays_normal():
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    await init_db()
    async with SessionLocal() as db_session:
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="cont1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.1,
            stop_loss=90.0,
            take_profit=110.0,
            execution_state="normal",
            monitor_from_ms=0,
            last_processed_candle_ms=t0,
        )
        db_session.add(pos)
        await db_session.commit()
        bar = Candle(time_ms=t1, open=100, high=101, low=99, close=100, volume=1.0)
        bundle = KlineBundle(
            closed=[bar], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m"
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.execution_state == "normal"
        assert pos.last_processed_candle_ms == t1
        assert pos.status == "open"


@pytest.mark.asyncio
async def test_internal_gap_blocks_definitive_tp():
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    t3 = t0 + 180_000
    await init_db()
    async with SessionLocal() as db_session:
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="igap1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.1,
            stop_loss=90.0,
            take_profit=105.0,
            execution_state="normal",
            monitor_from_ms=0,
            last_processed_candle_ms=t0,
        )
        db_session.add(pos)
        await db_session.commit()
        b1 = Candle(time_ms=t1, open=100, high=101, low=99, close=100, volume=1.0)
        b3 = Candle(time_ms=t3, open=104, high=106, low=103, close=105, volume=1.0)
        bundle = KlineBundle(
            closed=[b1, b3],
            forming=None,
            gaps_detected=[t0 + 120_000],
            gaps_remaining=[t0 + 120_000],
            data_complete=False,
            interval="1m",
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.status == "open"
        assert pos.execution_state == "suspended"
        assert pos.last_processed_candle_ms == t1


@pytest.mark.asyncio
async def test_gap_recovery_then_tp_processed():
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    t2 = t0 + 120_000
    t3 = t0 + 180_000
    await init_db()
    async with SessionLocal() as db_session:
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="rec1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.1,
            stop_loss=90.0,
            take_profit=105.0,
            execution_state="suspended",
            monitor_from_ms=0,
            last_processed_candle_ms=t1,
            extra={"execution_suspended_reason": "internal_candle_gap"},
        )
        db_session.add(pos)
        await db_session.commit()
        bars = [
            Candle(time_ms=t2, open=100, high=101, low=99, close=100, volume=1.0),
            Candle(time_ms=t3, open=104, high=106, low=103, close=105, volume=1.0),
        ]
        bundle = KlineBundle(
            closed=bars, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m"
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.execution_state == "normal"
        assert pos.status == "tp_hit"


@pytest.mark.asyncio
async def test_monitor_restart_idempotent_no_duplicate_close():
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="idem1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.25,
            stop_loss=90.0,
            take_profit=105.0,
            monitor_from_ms=0,
            last_processed_candle_ms=t0,
        )
        db_session.add(pos)
        await db_session.commit()
        bar = Candle(time_ms=t1, open=100, high=106, low=99, close=104, volume=1.0)
        bundle = KlineBundle(
            closed=[bar], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m"
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(db_session)
            await mon.monitor_session(db_session)
        ledgers = (
            await db_session.execute(
                select(SimWalletLedger).where(SimWalletLedger.entry_type == wallet.LEDGER_REALIZED_PNL)
            )
        ).scalars().all()
        assert len(ledgers) == 1


def test_merge_open_form_preserves_ai_limit_prefill():
    form = {
        "direction": "LONG",
        "margin_usdt": "50",
        "leverage": "10",
        "order_type": "limit",
        "entry_price": "99000",
        "stop_loss": "98000",
        "take_profit": "101000",
        "ai_setup_id": "abc",
    }
    merged = merge_open_form_defaults(
        form,
        {"default_margin_usdt": 25, "default_leverage": 5, "default_order_type": "market"},
        preserve_execution=True,
    )
    assert merged["order_type"] == "limit"
    assert merged["entry_price"] == "99000"
    assert merged["stop_loss"] == "98000"
    assert merged["take_profit"] == "101000"
    assert merged["margin_usdt"] == "25"


@pytest.mark.asyncio
async def test_equity_unrealized_excludes_entry_fee():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(db_session, max_risk_pct_per_position=100.0, slippage_bps=0)
        svc = SimPositionService()
        req = OrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=50.0,
            leverage=10,
            order_type="market",
            entry_price=None,
            stop_loss=99.0,
            take_profit=110.0,
        )
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(100.0, None))):
            await svc.create_order(db_session, req)
        # The mark is explicit: this test checks equity arithmetic, not Bitunix HTTP.
        with patch.object(svc.market, "fetch_ticker", AsyncMock(return_value=(101.0, None))):
            mtm = await aggregate_open_mark_to_market(db_session, svc)
        summary = await wallet.wallet_summary(
            db_session,
            unrealized_gross=mtm["unrealized_gross"],
            estimated_exit_fees=mtm["estimated_exit_fees"],
        )
        pos = (await db_session.execute(select(SimPosition))).scalar_one()
        assert summary["wallet_balance"] == pytest.approx(1000.0 - pos.entry_fee_usdt, abs=1e-4)
        assert summary["account_equity"] == pytest.approx(
            summary["wallet_balance"] + mtm["unrealized_gross"] - mtm["estimated_exit_fees"],
            abs=1e-4,
        )
        assert mtm["unrealized_net"] == pytest.approx(mtm["unrealized_gross"] - mtm["estimated_exit_fees"], abs=1e-6)


@pytest.mark.asyncio
async def test_quote_outage_does_not_rollback_paper_limit_fill():
    """An MTM quote failure must not discard a candle-validated paper fill."""
    from httpx import ConnectError

    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="quote-outage-fill",
            symbol="BTCUSDT",
            direction="LONG",
            status="pending_entry",
            order_type="limit",
            margin_usdt=50.0,
            leverage=10,
            limit_entry_price=100.0,
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            monitor_from_ms=0,
            last_processed_candle_ms=0,
        )
        db_session.add(pos)
        await db_session.commit()
        candle = Candle(time_ms=60_000, open=100, high=101, low=99, close=100, volume=1)
        bundle = KlineBundle(
            closed=[candle], forming=None, gaps_detected=[], gaps_remaining=[],
            data_complete=True, interval="1m",
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch(
                "app.services.position.position_monitor.record_equity_snapshot",
                AsyncMock(side_effect=ConnectError("offline simulated quote")),
            ):
                await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.status == "open"
        assert pos.fill_price == pytest.approx(100.0)
        assert pos.extra.get("entry_fill_bar_ms") == candle.time_ms


@pytest.mark.asyncio
async def test_monitor_limit_fill_same_bar_tp_is_ambiguous_not_tp_hit():
    """LONG limit entry=100, TP=105, bar O=103,H=106,L=99,C=104 — no definitive TP on entry bar."""
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="ltp1",
            symbol="BTCUSDT",
            direction="LONG",
            status="pending_entry",
            order_type="limit",
            limit_entry_price=100.0,
            entry_price=100.0,
            margin_usdt=50.0,
            leverage=10,
            stop_loss=95.0,
            take_profit=105.0,
            monitor_from_ms=0,
            last_processed_candle_ms=0,
        )
        db_session.add(pos)
        await db_session.commit()
        bar = Candle(time_ms=60_000, open=103, high=106, low=99, close=104, volume=1.0)
        bundle = KlineBundle(
            closed=[bar], forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m"
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.status == "closed"
        assert pos.close_reason == "ambiguous_intrabar"
        assert pos.extra.get("settlement_policy") == "same_bar_entry_no_intrabar_sequence"
        assert pos.extra.get("outcome_valid") is False


@pytest.mark.asyncio
async def test_suspended_execution_skips_definitive_tp_until_history_recovered():
    await init_db()
    async with SessionLocal() as db_session:
        await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=1000.0)
        await _ensure_paper_settings(db_session, slippage_bps=0, max_risk_pct_per_position=100)
        pos = SimPosition(
            public_id="susp1",
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            order_type="market",
            margin_usdt=50.0,
            leverage=10,
            fill_price=100.0,
            entry_price=100.0,
            quantity_btc=5.0,
            entry_fee_usdt=0.1,
            stop_loss=90.0,
            take_profit=105.0,
            execution_state="suspended",
            monitor_from_ms=0,
            last_processed_candle_ms=1_000_000_000_000,
        )
        db_session.add(pos)
        await db_session.commit()
        tp_bar = Candle(time_ms=1_700_000_000_100, open=100, high=106, low=99, close=104, volume=1.0)
        gap_bundle = KlineBundle(
            closed=[tp_bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[1_700_000_000_050],
            data_complete=False,
            interval="1m",
        )
        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=gap_bundle)):
            await mon.monitor_session(db_session)
        await db_session.refresh(pos)
        assert pos.status == "open"
        assert pos.execution_state == "suspended"


def test_telegram_analysis_time_tehran():
    dt = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    cap = format_ai_chart_caption(
        symbol="BTCUSDT",
        interval="1h",
        decision="LONG",
        live_price=100.0,
        trade_levels={"entry": 100, "stop_loss": 90, "take_profit": 110},
        analyzed_at=dt,
    )
    assert "تهران" in cap
    assert format_tehran(dt) in cap
