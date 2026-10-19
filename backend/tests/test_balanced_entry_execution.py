from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup
from app.services.balanced_confirmation_engine import (
    advance_balanced_on_closed_5m_bar,
    pre_entry_requirements_met,
)
from app.services.conditional_revision import objective_confirmations_satisfied_for_order
from app.services.ict_confirmation_engine import process_bar_sequential
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from tests.test_confirmation_replay_chronology import (
    STEP1,
    STEP5,
    ZONE_HI,
    ZONE_LO,
    _build_positive_same_5m,
)


def test_balanced_records_all_tokens_on_one_5m_while_strict_records_one():
    closed_5m, closed_1m, bar_5m, now_ms = _build_positive_same_5m()
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    boundary = closed_5m[0].time_ms
    state: dict = {"confirmation_timestamps": {}, "confirmation_evidence": {}}

    strict = process_bar_sequential(
        direction="LONG",
        sequence=seq,
        confirmation_state=state,
        bar=bar_5m,
        window=closed_5m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=boundary,
    )
    assert len(strict.completed) == 1

    balanced = advance_balanced_on_closed_5m_bar(
        direction="LONG",
        sequence=seq,
        confirmation_state={"confirmation_timestamps": {}, "confirmation_evidence": {}},
        bar_5m=bar_5m,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=boundary,
        now_ms=now_ms,
    )
    assert balanced.completed == seq
    assert balanced.confirmation_timestamps["liquidity_sweep"] < balanced.confirmation_timestamps["mss"]
    assert balanced.confirmation_timestamps["mss"] < balanced.confirmation_timestamps["entry_zone_touch"]


def test_pre_entry_objective_allows_order_without_touch():
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    state = {
        "sequence": seq,
        "required": seq,
        "completed": ["liquidity_sweep", "mss"],
        "missing": ["entry_zone_touch"],
        "confirmation_timestamps": {"liquidity_sweep": 1, "mss": 2},
    }

    class Row:
        confirmation_state = state
        structured_snapshot = {}
        entry_zone_low = ZONE_LO
        entry_zone_high = ZONE_HI

    assert pre_entry_requirements_met(sequence=seq, confirmation_state=state, structured={})
    ok, reason = objective_confirmations_satisfied_for_order(Row(), allow_pre_entry_without_touch=True)
    assert ok is True
    assert reason is None


@pytest.mark.asyncio
async def test_balanced_shadow_does_not_create_order():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=20)
    boundary = first_eligible_bar_open_ms(created, "5m")
    closed_5m, closed_1m, bar_5m, _ = _build_positive_same_5m()
    # Align monitor boundary with synthetic scenario
    boundary = bar_5m.time_ms - 14 * STEP5
    now_ms = bar_5m.time_ms + STEP5 + 5_000

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = True
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="bal-shadow-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            setup_mode="conditional",
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            created_at=created,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=120.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": ["liquidity_sweep"],
                "missing": ["mss", "entry_zone_touch"],
                "confirmation_timestamps": {"liquidity_sweep": boundary},
            },
            conditional_plan={"confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"]},
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(
            closed=closed_1m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )

        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 30, 330),
                ):
                    with patch(
                        "app.services.opportunity_monitor._net_rr_ok",
                        AsyncMock(return_value=(True, 2.5)),
                    ):
                        summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        assert summary.get("paper_entry_mode") == ENTRY_MODE_BALANCED
        assert summary.get("orders", 0) == 0
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "bal-shadow-1"))).scalar_one()
        assert row.lifecycle_state == READY_LONG
        assert (row.last_validation_result or {}).get("shadow_mode") is True


@pytest.mark.asyncio
async def test_strict_mode_default_unchanged_in_runtime():
    await init_db()
    async with SessionLocal() as session:
        from app.services.entry_execution_settings import get_paper_entry_execution_config

        mode, shadow = await get_paper_entry_execution_config(session)
        assert mode == ENTRY_MODE_STRICT
        assert shadow is False
