from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
from app.domain.setup_lifecycle import READY_LONG
from app.models import AiProposedSetup, SimPositionSettings
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.paper_auto_trading import maybe_auto_open_paper_position
from app.services.position import wallet_service as wallet
from app.services.position.settings_service import get_or_create_settings
from app.services.runtime_settings import get_runtime_settings


@pytest.mark.asyncio
async def test_auto_paper_rejects_when_runtime_min_rr_below_2_but_costs_fail_hard_floor():
    await init_db()
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
        settings = await get_or_create_settings(session)
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        runtime = await get_runtime_settings(session)
        runtime.min_net_rr = 1.2
        await session.commit()

        setup = AiProposedSetup(
            setup_id="rr-tight",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=98.0,
            take_profit=103.5,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": ["entry_zone_touch"],
                "missing": [],
                "confirmation_timestamps": {"entry_zone_touch": 1_000_000},
                "confirmation_evidence": {
                    "entry_zone_touch": {
                        "bar_low": 99.5,
                        "bar_high": 100.5,
                        "zone_lo": 99.0,
                        "zone_hi": 101.0,
                    }
                },
            },
            structured_snapshot={
                "decision": "LONG",
                "liquidity_event_clear": True,
                "poi_reaction_clear": True,
                "structural_confirmation_clear": True,
                "logical_invalidation_clear": True,
            },
        )
        session.add(setup)
        await session.commit()

        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True},
            structured=setup.structured_snapshot,
            symbol="BTCUSDT",
            commit=False,
            require_ai_flags=True,
        )
        assert meta["skipped"] is True
        assert meta["reason"] == "net_rr_below_min"
        assert meta["required_min_net_rr"] == HARD_MIN_AUTO_PAPER_NET_RR


@pytest.mark.asyncio
async def test_opportunity_monitor_promotion_blocked_when_net_rr_under_2():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="rr-mon",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="WAIT_LONG",
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=-1,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=98.0,
            take_profit=103.0,
            invalidation_level=97.0,
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": ["entry_zone_touch"],
                "missing": [],
                "confirmation_timestamps": {"entry_zone_touch": 300_000},
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

        mon = OpportunityMonitorService()
        from app.domain.candles import Candle
        from app.services.market_data import KlineBundle

        bundle = KlineBundle(
            closed=[Candle(time_ms=300_000, open=100, high=101, low=99.5, close=100.2, volume=1)],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        with patch.object(mon.market, "fetch_kline_bundle", return_value=bundle):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(600_000 / 1000, tz=timezone.utc)
                summary = await mon.monitor_setups(session)
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "rr-mon"))).scalar_one()
        assert summary["promoted"] == 0
        assert row.lifecycle_state == "WAIT_LONG"
