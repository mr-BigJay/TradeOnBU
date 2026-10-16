from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import INVALIDATED, READY_LONG
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet


@pytest.mark.asyncio
async def test_ready_setup_invalidated_on_5m_gap_before_any_paper_order():
    await init_db()
    now = datetime.now(timezone.utc)
    inv_open = 1_700_000_000_000
    monitor_at = datetime.fromtimestamp((inv_open + 300_000 + 10_000) / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=20_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True

        setup = AiProposedSetup(
            setup_id="ready-inv-gap",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=inv_open - 300_000,
            monitor_cursor_ms=inv_open - 600_000,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=12),
            last_validation_result={"ok": True, "net_rr": 2.5},
            structured_snapshot={"decision": "LONG", "final_ai_decision": "LONG"},
            conditional_plan={"confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"]},
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "missing": [],
                "confirmation_timestamps": {
                    "liquidity_sweep": inv_open - 900_000,
                    "mss": inv_open - 600_000,
                    "entry_zone_touch": inv_open - 300_000,
                },
                "confirmation_evidence": {
                    "entry_zone_touch": {
                        "bar_low": 99.5,
                        "bar_high": 100.5,
                        "zone_lo": 99.0,
                        "zone_hi": 101.0,
                    }
                },
            },
        )
        session.add(setup)
        await session.commit()

    inv_bar = Candle(time_ms=inv_open, open=95.0, high=96.0, low=91.0, close=92.0, volume=1.0)
    bundle = KlineBundle(
        closed=[inv_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    mon = OpportunityMonitorService()
    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_at):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ready-inv-gap"))
        ).scalar_one()
        pos_count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        summary_wallet = await wallet.wallet_summary(session)
        assert summary.get("invalidated", 0) >= 1
        assert summary.get("orders", 0) == 0
        assert row.lifecycle_state == INVALIDATED
        assert pos_count == 0
        assert summary_wallet["margin_in_use"] == 0.0
