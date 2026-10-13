"""Final correctness pass regression tests for PR #4."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.models import AiAnalysisSettings, AiProposedSetup
from app.services.ai_analysis_scheduler import FAILURE_BACKOFF_MINUTES, record_run_outcome, should_run_scheduled
from app.services.ai_setup_memory import (
    LIFECYCLE_ENTRY_TOUCHED,
    LIFECYCLE_SAME_CANDLE_AMBIGUOUS,
    LIFECYCLE_UNKNOWN_MISSING_DATA,
    apply_objective_price_tracking,
    create_setup_from_structured,
    refresh_setup_objective_state,
)
from app.services.market_data import KlineBundle


def test_same_candle_ambiguous_not_reclassified_on_reprocess():
    setup = AiProposedSetup(
        setup_id="amb",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
        last_tracked_candle_ms=0,
    )
    candle = Candle(open=100, high=100.4, low=97.0, close=99.0, time_ms=1000)
    apply_objective_price_tracking(setup, [candle], watermark_ms=0)
    assert setup.sequencing_ambiguous is True
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_SAME_CANDLE_AMBIGUOUS
    assert not setup.sl_touched
    setup.last_tracked_candle_ms = 1000
    apply_objective_price_tracking(setup, [candle], watermark_ms=1000)
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_SAME_CANDLE_AMBIGUOUS
    assert not setup.sl_touched


def test_post_entry_sl_tp_same_candle_is_ambiguous():
    setup = AiProposedSetup(
        setup_id="post-amb",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=98.0,
        take_profit=105.0,
        entry_touched=True,
        created_at=datetime.now(timezone.utc),
        objective_notes={"lifecycle_phase": LIFECYCLE_ENTRY_TOUCHED},
    )
    candle = Candle(open=100, high=106, low=97.0, close=100.5, time_ms=2000)
    apply_objective_price_tracking(setup, [candle], watermark_ms=0)
    assert setup.sequencing_ambiguous is True
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_SAME_CANDLE_AMBIGUOUS
    assert not setup.sl_touched
    assert not setup.tp_touched


@pytest.mark.asyncio
async def test_expiry_after_final_tracking_window(session):
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    setup = AiProposedSetup(
        setup_id="exp-entry",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        created_at=created,
        expires_at=expires,
        last_tracked_candle_ms=0,
    )
    session.add(setup)
    await session.commit()

    entry_candle = Candle(
        open=100,
        high=100.5,
        low=99.8,
        close=100.2,
        time_ms=int((expires - timedelta(minutes=10)).timestamp() * 1000),
    )
    bundle = KlineBundle(
        closed=[entry_candle],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )

    async def fake_hist(_symbol, _interval, from_ms, max_bars=500):
        return bundle

    market = AsyncMock()
    market.fetch_historical_closed = fake_hist
    open_rows = await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.entry_touched is True
    assert setup.status == "expired"
    assert setup.objective_notes.get("lifecycle_phase") == "expired"
    assert "exp-entry" not in [s.setup_id for s in open_rows]


@pytest.mark.asyncio
async def test_gap_does_not_advance_watermark_past_unresolved(session):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    base_ms = int(created.timestamp() * 1000)
    setup = AiProposedSetup(
        setup_id="gap-wm",
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
    c1 = Candle(open=100, high=100.2, low=99.8, close=100.0, time_ms=base_ms + 600_000)
    gap_start = base_ms + 900_000
    bundle = KlineBundle(
        closed=[c1],
        forming=None,
        gaps_detected=[gap_start],
        gaps_remaining=[gap_start],
        data_complete=False,
        interval="5m",
    )

    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.last_tracked_candle_ms == c1.time_ms
    assert setup.objective_notes.get("outcome_tracking_status") == "incomplete"
    assert setup.objective_notes.get("pending_tracking_from_ms") is not None


@pytest.mark.asyncio
async def test_gap_recovery_continues_from_pending(session):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    base_ms = int(created.timestamp() * 1000)
    setup = AiProposedSetup(
        setup_id="gap-rec",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=95.0,
        take_profit=110.0,
        created_at=created,
        last_tracked_candle_ms=base_ms + 600_000,
        objective_notes={"pending_tracking_from_ms": base_ms + 900_000},
    )
    session.add(setup)
    await session.commit()
    c2 = Candle(open=100, high=100.5, low=99.6, close=100.2, time_ms=base_ms + 1_200_000)
    bundle = KlineBundle(
        closed=[c2],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.entry_touched is True
    assert setup.last_tracked_candle_ms == c2.time_ms


def test_scheduled_failure_one_retry_then_regular_interval():
    cfg = AiAnalysisSettings(interval_minutes=120, last_success_at=datetime.now(timezone.utc) - timedelta(hours=1))
    record_run_outcome(cfg, "failed", "err", trigger="scheduled")
    assert cfg.auto_retry_pending is True
    retry_at = cfg.next_run_at
    assert retry_at <= datetime.now(timezone.utc) + timedelta(minutes=FAILURE_BACKOFF_MINUTES + 1)
    record_run_outcome(cfg, "failed", "err", trigger="scheduled")
    assert cfg.auto_retry_pending is False
    assert cfg.next_run_at > retry_at + timedelta(minutes=30)


def test_manual_failure_does_not_alter_schedule():
    nr = datetime.now(timezone.utc) + timedelta(hours=2)
    cfg = AiAnalysisSettings(next_run_at=nr, auto_retry_pending=False)
    record_run_outcome(cfg, "failed", "err", trigger="manual")
    assert cfg.next_run_at == nr
    assert cfg.auto_retry_pending is False


@pytest.mark.asyncio
async def test_new_setup_true_still_dedupes_same_levels(session):
    base = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
        "proposed_setup": {"new_setup": True, "thesis_fa": "a"},
    }
    r1, m1 = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=base)
    await session.commit()
    assert m1["created"]
    base2 = dict(base)
    base2["proposed_setup"] = {"new_setup": True, "thesis_fa": "b"}
    r2, m2 = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=base2)
    assert m2["reason"] == "deduped_revision_appended"
    assert r2.setup_id == r1.setup_id


@pytest.mark.asyncio
async def test_continue_setup_rejects_cross_symbol(session):
    row = AiProposedSetup(
        setup_id="cid",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_price=100,
        stop_loss=98,
        take_profit=105,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    structured = {
        "decision": "LONG",
        "entry_price": 100,
        "stop_loss": 98,
        "take_profit": 105,
        "proposed_setup": {"continue_setup_id": "cid", "thesis_fa": "x"},
    }
    _, meta = await create_setup_from_structured(session, run_id=2, symbol="ETHUSDT", structured=structured)
    assert meta["reason"] == "continue_setup_mismatch"
