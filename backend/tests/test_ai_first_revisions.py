"""Regression tests for AI-first PR revisions (setup tracking, scheduler, legacy scan)."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.candles import Candle
from app.models import AiAnalysisSettings, AiProposedSetup
from app.services.ai_analysis_scheduler import migrate_ai_schedule_settings, record_run_outcome, should_run_scheduled
from app.services.ai_first_mode import legacy_rule_scan_enabled
from app.services.ai_setup_memory import (
    LIFECYCLE_ENTRY_TOUCHED,
    LIFECYCLE_SL_AFTER_ENTRY,
    LIFECYCLE_SL_BEFORE_ENTRY,
    LIFECYCLE_TP_BEFORE_ENTRY,
    apply_objective_price_tracking,
    create_setup_from_structured,
    validate_proposed_setup_levels,
)
from app.services.entry_detection import EntryDetectionService
from app.services.market_data import KlineBundle
from app.worker import BotWorker


def test_legacy_scan_disabled_in_ai_first_mode():
    assert legacy_rule_scan_enabled() is False


@pytest.mark.asyncio
async def test_worker_skips_rule_scan_but_can_monitor(session):
    worker = BotWorker()
    entry_scan = AsyncMock(return_value=None)
    # This test checks scan/monitor scheduling, not HTTP or price-based fills.
    with patch("app.worker.try_acquire_worker_lock", AsyncMock(return_value=True)):
        with patch("app.worker.release_worker_lock", AsyncMock()):
            with patch("app.services.opportunity_monitor.run_opportunity_monitor_tick", AsyncMock(return_value={})):
                with patch("app.services.position.position_monitor.SimPositionMonitorService.monitor_session", AsyncMock(return_value=0)):
                    with patch("app.services.signal_monitor.SignalMonitorService.monitor_open_signals", AsyncMock(return_value={})) as monitor:
                        with patch("app.services.notification_outbox.NotificationOutboxService.flush_pending", AsyncMock()):
                            with patch("app.worker.TrendEngine") as te_cls:
                                te_cls.return_value.analyze = AsyncMock()
                                te_cls.return_value.persist = AsyncMock()
                                with patch("app.worker.EntryDetectionService") as ed_cls:
                                    ed_cls.return_value.scan = entry_scan
                                    await worker.run_cycle()
    monitor.assert_awaited()
    entry_scan.assert_not_called()
    te_cls.return_value.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_entry_detection_scan_noop_when_ai_first():
    svc = EntryDetectionService(AsyncMock(), AsyncMock(), AsyncMock())
    assert await svc.scan("BTCUSDT", 2.0) is None


def test_sl_before_entry_not_post_entry_outcome():
    setup = AiProposedSetup(
        setup_id="s1",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
    )
    candles = [
        Candle(open=99.2, high=99.4, low=97.5, close=98.2, time_ms=1),
        Candle(open=98.5, high=100.6, low=99.6, close=100.1, time_ms=2),
    ]
    apply_objective_price_tracking(setup, candles)
    assert not setup.sl_touched
    assert setup.entry_touched is True
    notes = setup.objective_notes or {}
    assert notes.get("sl_pre_entry") is True
    assert notes.get("lifecycle_phase") == LIFECYCLE_ENTRY_TOUCHED
    assert "sl_pre_entry" in (notes.get("first_touch_ms") or {})


def test_post_entry_sl_after_entry_touched():
    setup = AiProposedSetup(
        setup_id="s2",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=105.0,
        entry_touched=True,
        created_at=datetime.now(timezone.utc),
        objective_notes={"lifecycle_phase": LIFECYCLE_ENTRY_TOUCHED},
    )
    candles = [Candle(open=100, high=100.2, low=97.5, close=98.2, time_ms=3)]
    apply_objective_price_tracking(setup, candles)
    assert setup.sl_touched is True
    assert setup.objective_notes.get("lifecycle_phase") == LIFECYCLE_SL_AFTER_ENTRY


def test_same_candle_entry_and_sl_ambiguous():
    setup = AiProposedSetup(
        setup_id="s3",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
    )
    candles = [Candle(open=100, high=100.4, low=97.0, close=99.0, time_ms=1)]
    apply_objective_price_tracking(setup, candles)
    assert setup.sequencing_ambiguous is True
    assert setup.objective_notes.get("lifecycle_phase") == "same_candle_ambiguous"


def test_tp_before_entry():
    setup = AiProposedSetup(
        setup_id="s4",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=101.0,
        created_at=datetime.now(timezone.utc),
    )
    candles = [Candle(open=100.8, high=101.5, low=100.6, close=101.2, time_ms=1)]
    apply_objective_price_tracking(setup, candles)
    assert not setup.tp_touched
    assert setup.objective_notes.get("tp_pre_entry") is True
    assert setup.objective_notes.get("lifecycle_phase") == LIFECYCLE_TP_BEFORE_ENTRY


@pytest.mark.asyncio
async def test_long_setup_tracking_uses_historical_backfill(session):
    from app.services.ai_setup_memory import refresh_setup_objective_state

    created = datetime.now(timezone.utc) - timedelta(hours=30)
    setup = AiProposedSetup(
        setup_id="long-life",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        created_at=created,
        last_tracked_candle_ms=0,
    )
    session.add(setup)
    await session.commit()

    old_candle = Candle(open=100, high=100.5, low=99.5, close=100.2, time_ms=int(created.timestamp() * 1000) + 3600_000)
    bundle = KlineBundle(
        closed=[old_candle],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )

    async def fake_hist(_symbol, _interval, from_ms, max_bars=500):
        assert from_ms >= int(created.timestamp() * 1000)
        return bundle

    market = AsyncMock()
    market.fetch_historical_closed = fake_hist
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.last_tracked_candle_ms == old_candle.time_ms


@pytest.mark.asyncio
async def test_closed_setups_in_memory_context(session):
    from app.services.ai_setup_memory import build_ai_memory_context

    closed = AiProposedSetup(
        setup_id="closed-1",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="expired",
        thesis_fa="قدیمی",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=105.0,
        closed_at=datetime.now(timezone.utc),
        objective_notes={"lifecycle_phase": "expired", "outcome_tracking_status": "complete"},
        last_tracked_candle_ms=1,
    )
    session.add(closed)
    await session.commit()
    ctx = await build_ai_memory_context(session, symbol="BTCUSDT", candles_by_interval={})
    assert any(s["setup_id"] == "closed-1" for s in ctx["recent_closed_setups"])


def test_token_budget_trims_finest_timeframe():
    from app.services.ai_market_payload import _apply_token_budget

    payload = {
        "timeframes": {
            "1m": {"candles": [{"time_ms": i, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(500)]},
            "4h": {"candles": [{"time_ms": 0, "open": 1, "high": 1, "low": 1, "close": 1}]},
        }
    }
    out = _apply_token_budget(payload, max_chars=2000)
    assert out["token_budget"]["estimated_chars_after"] <= 2000
    assert len(out["timeframes"]["1m"]["candles"]) < 500


@pytest.mark.asyncio
async def test_missing_candles_marks_unknown(session):
    setup = AiProposedSetup(
        setup_id="gap",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        created_at=datetime.now(timezone.utc),
    )
    bundle = KlineBundle(
        closed=[],
        forming=None,
        gaps_detected=[1],
        gaps_remaining=[1],
        data_complete=False,
        interval="5m",
    )
    apply_objective_price_tracking(setup, [], bundle=bundle)
    assert setup.objective_notes.get("lifecycle_phase") == "unknown_missing_data"


@pytest.mark.asyncio
async def test_duplicate_setup_suppressed(session):
    from tests.conftest import with_immediate_monitor_fields

    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True, "thesis_fa": "اول"},
        }
    )
    row1, meta1 = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=structured)
    await session.commit()
    assert meta1["created"] is True

    structured2 = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.05,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": False, "thesis_fa": "تکرار"},
        }
    )
    row2, meta2 = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=structured2)
    assert meta2.get("reason") == "deduped_revision_appended"
    assert row2.setup_id == row1.setup_id


@pytest.mark.asyncio
async def test_invalid_levels_not_persisted(session):
    from tests.conftest import with_immediate_monitor_fields

    bad = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 101.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True},
        }
    )
    row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=bad)
    assert row is None
    assert meta["reason"] == "long_levels_not_ordered_sl_entry_tp"
    assert validate_proposed_setup_levels("LONG", 100, 101, 105)[0] is False


def test_schedule_migration_restores_legacy_auto_one_twenty_once():
    nr = datetime.now(timezone.utc) + timedelta(minutes=25)
    cfg = AiAnalysisSettings(
        enabled=True,
        unattended_consent=True,
        interval_minutes=120,
        next_run_at=nr,
        interval_migrated_from_60=True,
    )
    assert migrate_ai_schedule_settings(cfg) is True
    assert cfg.interval_minutes == 60
    assert cfg.interval_migrated_from_60 is False
    assert migrate_ai_schedule_settings(cfg) is False


@pytest.mark.asyncio
async def test_failure_retry_single_backoff_not_hourly_loop(session):
    cfg = AiAnalysisSettings(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=120,
        interval_migrated_from_60=False,
    )
    session.add(cfg)
    await session.commit()
    record_run_outcome(cfg, "failed", "openai_error", trigger="scheduled")
    first_next = cfg.next_run_at
    assert cfg.auto_retry_pending is True
    assert await should_run_scheduled(session) is False
    record_run_outcome(cfg, "failed", "openai_error", trigger="scheduled")
    assert cfg.auto_retry_pending is False
    assert cfg.next_run_at > first_next + timedelta(minutes=60)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("SIWC_LIVE_MAX_TEST") != "1", reason="Set SIWC_LIVE_MAX_TEST=1 with valid SIWC token")
async def test_siwc_live_max_reasoning_smoke():
    """
    Live compatibility plan (manual/CI optional):
    1. Connect ChatGPT via SIWC on staging.
    2. export SIWC_LIVE_MAX_TEST=1 and ensure credentials exist in DB.
    3. Run this test — sends minimal Responses request with gpt-6.1-sol + max effort.
    4. Expect response.completed with confirmed reasoning effort max or explicit reasoning_max_unavailable.
    """
    from app.db import SessionLocal
    from app.services.siwc.credentials import get_access_token
    from app.services.siwc.responses_client import OpenAIResponsesError, stream_analysis_response

    async with SessionLocal() as session:
        token, _, err = await get_access_token(session)
    if err or not token:
        pytest.skip("SIWC not connected")
    try:
        result = await stream_analysis_response(
            token,
            model="gpt-6.1-sol",
            instructions="Reply with JSON only.",
            user_input='{"ping":true}',
            reasoning_effort="max",
            timeout=120.0,
        )
    except OpenAIResponsesError as exc:
        if exc.code in ("reasoning_max_unavailable", "model_downgraded", "reasoning_downgraded"):
            pytest.fail(f"Max explicitly rejected or downgraded: {exc.code}: {exc}")
        pytest.fail(f"Unexpected OpenAI error for Max compatibility check: {exc.code}: {exc}")
    assert result.requested_reasoning_effort == "max"
    if result.confirmed_reasoning_effort == "max":
        return
    if result.confirmed_reasoning_effort is None:
        pytest.xfail("Max requested successfully; response omitted reasoning effort metadata (unconfirmed)")
    pytest.fail(f"Max requested but response confirmed effort={result.confirmed_reasoning_effort}")
