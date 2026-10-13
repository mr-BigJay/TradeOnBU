"""Data-integrity regressions: contiguous gaps, expiry reconciliation, immutable forecasts."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.models import AiProposedSetup
from app.services.ai_setup_memory import (
    LIFECYCLE_SL_AFTER_ENTRY,
    LIFECYCLE_SL_BEFORE_ENTRY,
    LIFECYCLE_TP_AFTER_ENTRY,
    TRACKING_INCOMPLETE,
    VALIDITY_EXPIRED,
    _verified_contiguous_prefix,
    apply_objective_price_tracking,
    create_setup_from_structured,
    refresh_setup_objective_state,
)
from app.services.market_data import KlineBundle


def test_verified_prefix_stops_at_first_gap():
    base = 1_000_000_000_000
    c1 = Candle(open=100, high=101, low=99, close=100, time_ms=base)
    c2 = Candle(open=100, high=106, low=99, close=105, time_ms=base + 900_000)
    bundle = KlineBundle(
        closed=[c1, c2],
        forming=None,
        gaps_detected=[base + 600_000],
        gaps_remaining=[base + 600_000],
        data_complete=False,
        interval="5m",
    )
    prefix = _verified_contiguous_prefix([c1, c2], bundle)
    assert [c.time_ms for c in prefix] == [base]
    assert c2 not in prefix


@pytest.mark.asyncio
async def test_post_gap_tp_ignored_until_gap_recovered_then_sl_ordering(session):
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    base_ms = int(created.timestamp() * 1000)
    setup = AiProposedSetup(
        setup_id="gap-order",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=created,
        last_tracked_candle_ms=0,
    )
    session.add(setup)
    await session.commit()

    c_entry = Candle(open=100, high=100.5, low=99.5, close=100.2, time_ms=base_ms + 600_000)
    c_sl = Candle(open=99, high=99.5, low=97.5, close=98, time_ms=base_ms + 750_000)
    c_tp_after_gap = Candle(open=104, high=106, low=103, close=105, time_ms=base_ms + 1_200_000)
    gap_at = base_ms + 900_000
    bundle1 = KlineBundle(
        closed=[c_entry, c_tp_after_gap],
        forming=None,
        gaps_detected=[gap_at],
        gaps_remaining=[gap_at],
        data_complete=False,
        interval="5m",
    )

    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle1)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.entry_touched is True
    assert not setup.tp_touched
    assert setup.objective_notes.get("lifecycle_phase") != LIFECYCLE_TP_AFTER_ENTRY

    bundle2 = KlineBundle(
        closed=[c_entry, c_sl, c_tp_after_gap],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    setup.last_tracked_candle_ms = 0
    setup.entry_touched = False
    setup.objective_notes = {"lifecycle_phase": "entry_not_reached", "outcome_tracking_status": TRACKING_INCOMPLETE}
    market.fetch_historical_closed = AsyncMock(return_value=bundle2)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.objective_notes.get("lifecycle_phase") == LIFECYCLE_SL_AFTER_ENTRY
    assert not setup.tp_touched


@pytest.mark.asyncio
async def test_expired_validity_keeps_tracking_until_reconciled(session):
    created = datetime.now(timezone.utc) - timedelta(hours=3)
    expires = datetime.now(timezone.utc) - timedelta(minutes=30)
    base_ms = int(created.timestamp() * 1000)
    setup = AiProposedSetup(
        setup_id="exp-recon",
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
        objective_notes={"outcome_tracking_status": TRACKING_INCOMPLETE},
    )
    session.add(setup)
    await session.commit()

    gap_at = base_ms + 900_000
    bundle = KlineBundle(
        closed=[Candle(open=100, high=100.2, low=99.8, close=100, time_ms=base_ms + 600_000)],
        forming=None,
        gaps_detected=[gap_at],
        gaps_remaining=[gap_at],
        data_complete=False,
        interval="5m",
    )
    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.objective_notes.get("validity_status") == VALIDITY_EXPIRED
    assert setup.status == "open"
    assert setup.objective_notes.get("outcome_tracking_status") == TRACKING_INCOMPLETE


@pytest.mark.asyncio
async def test_continue_preserves_original_forecast(session):
    row = AiProposedSetup(
        setup_id="orig",
        run_id=10,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        thesis_fa="original thesis",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    structured = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 106.0,
        "proposed_setup": {"continue_setup_id": "orig", "thesis_fa": "revised thesis"},
    }
    _, meta = await create_setup_from_structured(session, run_id=99, symbol="BTCUSDT", structured=structured)
    await session.refresh(row)
    assert meta["reason"] == "revision_appended"
    assert row.run_id == 10
    assert row.thesis_fa == "original thesis"
    assert row.stop_loss == 98.0
    orig = row.objective_notes["original_forecast"]
    assert orig["run_id"] == 10
    assert len(row.objective_notes["revision_history"]) == 1
    assert row.objective_notes["revision_history"][0]["source_run_id"] == 99


@pytest.mark.asyncio
async def test_dedupe_preserves_original_run_id(session):
    structured = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
        "proposed_setup": {"new_setup": True, "thesis_fa": "first"},
    }
    r1, _ = await create_setup_from_structured(session, run_id=5, symbol="BTCUSDT", structured=structured)
    await session.commit()
    structured2 = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
        "proposed_setup": {"new_setup": True, "thesis_fa": "second"},
    }
    _, meta = await create_setup_from_structured(session, run_id=6, symbol="BTCUSDT", structured=structured2)
    await session.refresh(r1)
    assert meta["reason"] == "deduped_revision_appended"
    assert r1.run_id == 5
    assert r1.thesis_fa == "first"
