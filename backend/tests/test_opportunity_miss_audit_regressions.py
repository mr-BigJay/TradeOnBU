from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.candles import Candle
from app.domain.opportunity_miss_reasons import (
    OUTCOME_ENGINE_DELAY,
    OUTCOME_INSUFFICIENT_REPLAY_HISTORY,
    OUTCOME_INTENTIONAL_WAIT,
    OUTCOME_ORDER_CREATED,
    OUTCOME_POTENTIAL_MISSED,
    REPLAY_STATUS_INSUFFICIENT_HISTORY,
    REPLAY_STATUS_OK,
)
from app.domain.setup_lifecycle import REASON_MISSING_CONFIRMATIONS, WAIT_LONG
from app.services.confirmation_replay import (
    assess_replay_history,
    chronology_provable_on_1m,
    diff_live_vs_replay,
    replay_confirmations_on_5m_bars,
)
from app.services.ict_confirmation_engine import ConfirmationProgress
from app.services.monitor_kline_backfill import first_discontinuity_open_ms
from app.services.opportunity_miss_report import classify_setup_outcome


def _candle(open_ms: int, *, o=100.0, h=101.0, l=99.0, c=100.5) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def test_replay_from_boundary_finds_confirmations_when_cursor_ahead():
    boundary = 1_000_000
    bars = [_candle(boundary), _candle(boundary + 300_000), _candle(boundary + 600_000)]
    replay = replay_confirmations_on_5m_bars(
        direction="LONG",
        sequence=["entry_zone_touch"],
        closed_bars=bars,
        zone_lo=99.0,
        zone_hi=101.0,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=boundary,
    )
    assert replay.confirmation_timestamps.get("entry_zone_touch") == boundary + 600_000 or replay.completed


def test_diff_untrusted_when_insufficient_history():
    replay = ConfirmationProgress(
        sequence=["a"],
        confirmation_timestamps={"a": 1000},
    )
    diff = diff_live_vs_replay(
        setup_id="x",
        live_state={"confirmation_timestamps": {}},
        replay=replay,
        replay_status=REPLAY_STATUS_INSUFFICIENT_HISTORY,
        replay_status_notes=["no_bars"],
    )
    assert diff.missing_in_live == []
    assert diff.replay_status == REPLAY_STATUS_INSUFFICIENT_HISTORY


def test_classify_potential_missed_requires_trusted_replay():
    setup = SimpleNamespace(
        lifecycle_state=WAIT_LONG,
        last_rejection_reason=REASON_MISSING_CONFIRMATIONS,
        objective_notes={},
        last_validation_result={},
    )
    untrusted = {"missing_in_live": ["mss"], "replay_status": REPLAY_STATUS_INSUFFICIENT_HISTORY}
    assert (
        classify_setup_outcome(setup, timeline={"paper_orders": []}, replay_diff=untrusted)
        == OUTCOME_INSUFFICIENT_REPLAY_HISTORY
    )
    trusted = {
        "missing_in_live": ["mss"],
        "replay_status": REPLAY_STATUS_OK,
        "replay_trusted": True,
    }
    assert (
        classify_setup_outcome(setup, timeline={"paper_orders": []}, replay_diff=trusted)
        == OUTCOME_POTENTIAL_MISSED
    )


def test_classify_closed_with_fill_is_order_created():
    setup = SimpleNamespace(
        lifecycle_state="POSITION_CLOSED",
        last_rejection_reason="",
        objective_notes={},
        last_validation_result={},
    )
    timeline = {"paper_orders": [{"status": "tp_hit", "fill_price": 101.0}]}
    assert classify_setup_outcome(setup, timeline=timeline, replay_diff=None) == OUTCOME_ORDER_CREATED


def test_classify_ready_without_delay_not_engine_delay():
    setup = SimpleNamespace(
        lifecycle_state="READY_LONG",
        last_rejection_reason="",
        objective_notes={},
        last_validation_result={"ok": True},
    )
    base_ms = 1_700_000_000_000
    timeline = {
        "final_confirmation_bar_close_ms": base_ms,
        "ready_at": datetime.fromtimestamp((base_ms + 30_000) / 1000, tz=timezone.utc).isoformat(),
    }
    assert classify_setup_outcome(setup, timeline=timeline, replay_diff=None) == OUTCOME_INTENTIONAL_WAIT


def test_classify_ready_with_delay_is_engine_delay():
    setup = SimpleNamespace(
        lifecycle_state="READY_LONG",
        last_rejection_reason="",
        objective_notes={},
        last_validation_result={"ok": True},
    )
    base_ms = 1_700_000_000_000
    timeline = {
        "final_confirmation_bar_close_ms": base_ms,
        "ready_at": datetime.fromtimestamp((base_ms + 200_000) / 1000, tz=timezone.utc).isoformat(),
    }
    assert classify_setup_outcome(setup, timeline=timeline, replay_diff=None) == OUTCOME_ENGINE_DELAY


def test_first_discontinuity_detects_missing_bar():
    step = 300_000
    cursor = 1_000_000
    new_bars = [_candle(cursor + 2 * step)]
    jump = first_discontinuity_open_ms(
        new_bars=new_bars,
        cursor_ms=cursor,
        boundary_ms=cursor,
    )
    assert jump == 1_200_000


def test_assess_replay_history_flags_missing_boundary_coverage():
    status, notes, seed = assess_replay_history(
        closed_bars=[_candle(2_000_000)],
        monitor_from_ms=1_000_000,
        monitor_cursor_ms=1_500_000,
        now_ms=3_000_000,
        data_complete=True,
        gaps_remaining=[],
        objective_notes={},
    )
    assert status == REPLAY_STATUS_INSUFFICIENT_HISTORY
    assert notes
    assert seed is None


def test_chronology_false_when_1m_incomplete():
    bar5 = _candle(1_000_000)
    ok, reason = chronology_provable_on_1m(
        direction="LONG",
        sequence=["entry_zone_touch", "mss"],
        hits_on_5m=["entry_zone_touch", "mss"],
        bar_5m=bar5,
        closed_5m=[bar5],
        closed_1m=[_candle(1_000_000), _candle(1_060_000)],
        zone_lo=99.0,
        zone_hi=101.0,
        structured={},
        now_ms=1_500_000,
    )
    assert ok is False
    assert reason == "incomplete_1m_coverage"


@pytest.mark.asyncio
async def test_monitor_suspends_cursor_on_gap():
    from app.db import SessionLocal, init_db
    from app.domain.setup_lifecycle import WAIT_LONG
    from app.models import AiProposedSetup
    from app.services.market_data import KlineBundle
    from app.services.opportunity_monitor import OpportunityMonitorService

    await init_db()
    boundary = 1_000_000
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="gap-suspend",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=boundary,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {},
            },
            structured_snapshot={},
        )
        session.add(setup)
        await session.commit()
        cursor_before = setup.monitor_cursor_ms

        # Missing bar at boundary+300k; only boundary+600k present
        bundle = KlineBundle(
            closed=[_candle(boundary), _candle(boundary + 600_000)],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        now_ms = boundary + 900_000 + 10_000
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch(
                "app.services.opportunity_monitor.fetch_monitor_5m_bundle",
                AsyncMock(return_value=(bundle, {"aggregate_limit": 120, "per_setup": []})),
            ):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    await mon.monitor_setups(session, symbol="BTCUSDT")

        await session.refresh(setup)
        assert setup.monitor_cursor_ms == cursor_before
