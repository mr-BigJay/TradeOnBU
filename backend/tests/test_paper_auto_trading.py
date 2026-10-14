from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.paper_auto_trading import maybe_auto_open_paper_position, setup_eligible_for_auto_entry
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService


async def _wallet_and_settings(db_session, *, auto: bool = True) -> SimPositionSettings:
    await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
    row = (await db_session.execute(select(SimPositionSettings))).scalar_one_or_none()
    if not row:
        row = SimPositionSettings(id=1)
        db_session.add(row)
    row.initial_balance_set = True
    row.auto_open_paper_positions = auto
    row.default_margin_usdt = 50.0
    row.default_leverage = 10
    row.max_risk_pct_per_position = 100.0
    await db_session.commit()
    return row


def _sample_setup(**kwargs) -> AiProposedSetup:
    now = datetime.now(timezone.utc)
    defaults = dict(
        setup_id="setup-auto-1",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        expires_at=now + timedelta(hours=24),
        paper_fill_recorded=False,
    )
    defaults.update(kwargs)
    return AiProposedSetup(**defaults)


@pytest.mark.asyncio
async def test_auto_open_creates_pending_limit_order():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup()
        session.add(setup)
        await session.flush()

        validation = {"ai_decision": "LONG", "acceptable_rr": True}
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=99,
            setup=setup,
            validation=validation,
            symbol="BTCUSDT",
            commit=True,
        )
        assert meta["created"] is True
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup.setup_id))
        ).scalar_one()
        assert pos.status == "pending_entry"
        assert pos.order_type == "limit"
        assert pos.limit_entry_price == 100.0
        assert pos.stop_loss == 95.0
        assert pos.take_profit == 110.0
        assert pos.extra.get("auto_paper") is True


@pytest.mark.asyncio
async def test_auto_open_skipped_when_disabled():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=False)
        setup = _sample_setup()
        session.add(setup)
        await session.flush()
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True},
            symbol="BTCUSDT",
        )
        assert meta["skipped"] is True
        assert meta["reason"] == "auto_open_disabled"


@pytest.mark.asyncio
async def test_auto_open_skipped_when_rr_not_acceptable():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup()
        session.add(setup)
        await session.flush()
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": False},
            symbol="BTCUSDT",
        )
        assert meta["reason"] == "net_rr_not_acceptable"


def test_setup_expired_not_eligible():
    setup = _sample_setup(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    ok, reason = setup_eligible_for_auto_entry(setup)
    assert ok is False
    assert reason == "setup_expired"


@pytest.mark.asyncio
async def test_monitor_cancels_expired_auto_pending_before_fill():
    await init_db()
    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=True)
        setup = _sample_setup(expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        session.add(setup)
        session.add(
            SimPosition(
                public_id="auto-pending-1",
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
                ai_setup_id=setup.setup_id,
                monitor_from_ms=0,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

        from app.domain.candles import Candle
        from app.services.market_data import KlineBundle

        bar = Candle(
            time_ms=1_700_000_000_000,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
        )
        bundle = KlineBundle(
            closed=[bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )

        mon = SimPositionMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            await mon.monitor_session(session)
        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert pos.status == "cancelled"
