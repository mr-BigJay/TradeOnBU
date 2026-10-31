from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import INVALIDATED, PENDING_ENTRY
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet

INTERVAL_1M = 60_000


@pytest.mark.asyncio
async def test_pending_cancelled_on_structural_invalidation_with_margin_release():
    await init_db()
    now = datetime.now(timezone.utc)
    monitor_now = now + timedelta(minutes=6)
    now_ms = int(monitor_now.timestamp() * 1000)
    bar_open = ((now_ms - 400_000) // 300_000) * 300_000
    order_ms = bar_open - 60_000

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True

        setup = AiProposedSetup(
            setup_id="inv-pending",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            monitor_from_ms=bar_open,
            monitor_cursor_ms=bar_open - 300_000,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=monitor_now + timedelta(hours=12),
            last_validation_result={"ok": True},
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="p-inv",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="inv-pending",
                monitor_from_ms=order_ms,
                extra={"auto_paper": True},
            )
        )
        await session.commit()
    inv_bar = Candle(time_ms=bar_open, open=92.5, high=93.5, low=91.0, close=92.0, volume=1.0)
    bundle_5m = KlineBundle(
        closed=[inv_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    inv_close = bar_open + 300_000
    pre_1m = []
    first_1m_open = (order_ms // INTERVAL_1M) * INTERVAL_1M
    t = first_1m_open
    while t <= inv_close - INTERVAL_1M:
        pre_1m.append(
            Candle(time_ms=t, open=101.5, high=102.0, low=100.5, close=101.5, volume=1.0)
        )
        t += 60_000
    bundle_1m = KlineBundle(
        closed=pre_1m,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )

    async def _fetch(_symbol: str, interval: str, _limit: int):
        return bundle_5m if interval == "5m" else bundle_1m

    mon = OpportunityMonitorService()
    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_now):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        assert summary.get("invalidated") == 1
        setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "inv-pending"))).scalar_one()
        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert setup.lifecycle_state == INVALIDATED
        assert pos.status == "cancelled"
        summary = await wallet.wallet_summary(session)
        assert summary["margin_in_use"] == 0.0
