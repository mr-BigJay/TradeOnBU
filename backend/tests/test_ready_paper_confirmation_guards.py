"""Paper order gates must not bypass objective ICT confirmations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.setup_lifecycle import PENDING_ENTRY, READY_LONG, READY_SHORT, WAIT_LONG, WAIT_SHORT
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.conditional_revision import RevisionImpact
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.paper_auto_trading import maybe_auto_open_paper_position
from app.services.position import wallet_service as wallet
from tests.test_conditional_revision_lifecycle import _short_setup_structured
from tests.test_lifecycle_realistic_pipeline import _5m, _sweep_mss_poi_long_bars
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long


def _valid_short_confirmation_state(*, boundary: int) -> dict:
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    return {
        "sequence": seq,
        "required": seq,
        "completed": list(seq),
        "missing": [],
        "confirmation_timestamps": {
            "liquidity_sweep": boundary,
            "mss": boundary + 300_000,
            "entry_zone_touch": boundary + 600_000,
        },
        "confirmation_evidence": {
            "entry_zone_touch": {
                "bar_low": 82310.0,
                "bar_high": 82390.0,
                "zone_lo": 82300.0,
                "zone_hi": 82400.0,
            }
        },
    }


@pytest.mark.asyncio
async def test_ready_demoted_after_none_revision_without_touch_evidence():
    await init_db()
    s1 = _short_setup_structured()
    s2 = _short_setup_structured(trade_reason="same thesis")
    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=s1)
        row.lifecycle_state = READY_SHORT
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": 100,
                "mss": 200,
                "entry_zone_touch": 300,
            },
        }
        await session.commit()
        row2, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=s2)
        await session.commit()
        assert meta.get("revision_impact") == RevisionImpact.NONE.value
        assert row2.lifecycle_state == WAIT_SHORT
        assert row2.last_rejection_reason == "missing_confirmations"


@pytest.mark.asyncio
async def test_ready_incomplete_confirmations_no_paper_order_with_auto_on():
    await init_db()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = SimPositionSettings(id=1, initial_balance_set=True, auto_open_paper_positions=True)
        session.add(settings)
        setup = AiProposedSetup(
            setup_id="ready-incomplete",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=0,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=12),
            conditional_plan={"confirmation_sequence": ["entry_zone_touch"]},
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {},
            },
            structured_snapshot={"decision": "LONG"},
        )
        session.add(setup)
        await session.commit()

        bundle = KlineBundle(
            closed=[_5m(300_000, o=100, h=100.5, l=99.5, c=100.2)],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=now + timedelta(minutes=6)):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ready-incomplete"))).scalar_one()
        assert row.lifecycle_state == WAIT_LONG
        assert (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one() == 0


@pytest.mark.asyncio
async def test_valid_ready_still_creates_paper_order():
    await init_db()
    created = datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc)
    boundary = first_eligible_bar_open_ms(created, "5m")
    closed = _sweep_mss_poi_long_bars(boundary)
    now_ms = candle_close_ms(closed[-1], "5m") + 15_000
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = SimPositionSettings(
            id=1,
            initial_balance_set=True,
            auto_open_paper_positions=True,
            max_risk_pct_per_position=100.0,
            default_margin_usdt=50.0,
            default_leverage=10,
        )
        session.add(settings)
        from app.services.ai_setup_memory import create_setup_from_structured

        row, _ = await create_setup_from_structured(
            session, run_id=10, symbol="BTCUSDT", structured=_production_conditional_wait_long()
        )
        row.created_at = created
        row.monitor_from_ms = boundary
        row.monitor_cursor_ms = boundary - 1
        row.lifecycle_state = "READY_LONG"
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": boundary,
                "mss": boundary + 300_000,
                "entry_zone_touch": boundary + 600_000,
            },
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 99.8,
                    "bar_high": 100.6,
                    "zone_lo": 99.0,
                    "zone_hi": 101.0,
                }
            },
        }
        row.structured_snapshot = {
            **_production_conditional_wait_long(),
            "decision": "LONG",
            "liquidity_event_clear": True,
            "poi_reaction_clear": True,
            "structural_confirmation_clear": True,
            "logical_invalidation_clear": True,
        }
        await session.commit()

        bundle = KlineBundle(
            closed=closed,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch.object(mon.market, "fetch_ticker", AsyncMock(return_value=(100.25, now_ms))):
                with patch("app.services.opportunity_monitor._utc_now", return_value=now_dt):
                    with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                        with patch("app.services.opportunity_monitor._net_rr_ok", AsyncMock(return_value=(True, 2.2))):
                            summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
        assert summary["orders"] >= 1
        assert row.lifecycle_state == PENDING_ENTRY


@pytest.mark.asyncio
async def test_maybe_auto_open_blocks_incomplete_even_with_require_ai_flags_false():
    await init_db()
    async with SessionLocal() as session:
        settings = SimPositionSettings(id=1, initial_balance_set=True, auto_open_paper_positions=True)
        session.add(settings)
        setup = AiProposedSetup(
            setup_id="gate-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            confirmation_state={"sequence": ["entry_zone_touch"], "missing": ["entry_zone_touch"], "completed": []},
        )
        session.add(setup)
        await session.commit()
        meta = await maybe_auto_open_paper_position(
            session,
            run_id=1,
            setup=setup,
            validation={"ai_decision": "LONG", "acceptable_rr": True, "objective_ready": True},
            structured={"decision": "LONG"},
            symbol="BTCUSDT",
            commit=False,
            require_ai_flags=False,
        )
        assert meta.get("created") is False
        assert meta.get("reason") == "missing_confirmations"
