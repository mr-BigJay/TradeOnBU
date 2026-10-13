"""Data-integrity regressions: contiguous gaps, expiry reconciliation, immutable forecasts."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.models import AiProposedSetup
from app.services.ai_setup_memory import (
    LIFECYCLE_SAME_CANDLE_AMBIGUOUS,
    LIFECYCLE_SL_AFTER_ENTRY,
    LIFECYCLE_TP_AFTER_ENTRY,
    TRACKING_COMPLETE,
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

    step = 300_000
    c_entry = Candle(open=100, high=100.5, low=99.5, close=100.2, time_ms=base_ms + step)
    c_sl = Candle(open=99, high=99.5, low=97.5, close=98, time_ms=base_ms + 2 * step)
    c_tp_after_gap = Candle(open=104, high=106, low=103, close=105, time_ms=base_ms + 5 * step)
    gap_at = base_ms + 3 * step
    bundle1 = KlineBundle(
        closed=[c_entry, c_tp_after_gap],
        forming=None,
        gaps_detected=[gap_at],
        gaps_remaining=[gap_at],
        data_complete=False,
        interval="5m",
    )

    bundle2 = KlineBundle(
        closed=[c_entry, c_sl, c_tp_after_gap],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(side_effect=[bundle1, bundle2])
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.entry_touched is True
    assert not setup.tp_touched
    assert setup.objective_notes.get("lifecycle_phase") != LIFECYCLE_TP_AFTER_ENTRY
    assert setup.objective_notes.get("outcome_tracking_status") == TRACKING_INCOMPLETE

    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.objective_notes.get("lifecycle_phase") == LIFECYCLE_SL_AFTER_ENTRY
    assert not setup.tp_touched
    assert setup.objective_notes.get("outcome_tracking_status") == "complete"
    assert setup.objective_notes.get("fetch_error") is None
    assert setup.objective_notes.get("pending_tracking_from_ms") is None


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


def _long_setup(**kwargs) -> AiProposedSetup:
    defaults = dict(
        setup_id="t",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return AiProposedSetup(**defaults)


def test_terminal_tp_immutable_after_later_sl():
    setup = _long_setup(entry_touched=True, objective_notes={"lifecycle_phase": "entry_touched"})
    apply_objective_price_tracking(
        setup,
        [
            Candle(open=104, high=106, low=103, close=105, time_ms=1000),
            Candle(open=100, high=100.5, low=97, close=98, time_ms=2000),
        ],
        watermark_ms=0,
    )
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_TP_AFTER_ENTRY
    assert setup.tp_touched is True


def test_terminal_sl_immutable_after_later_tp():
    setup = _long_setup(entry_touched=True, objective_notes={"lifecycle_phase": "entry_touched"})
    apply_objective_price_tracking(
        setup,
        [
            Candle(open=99, high=99.5, low=97.5, close=98, time_ms=1000),
            Candle(open=104, high=106, low=103, close=105, time_ms=2000),
        ],
        watermark_ms=0,
    )
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_SL_AFTER_ENTRY
    assert not setup.tp_touched


def test_same_candle_ambiguous_stable_on_refresh():
    setup = _long_setup()
    candle = Candle(open=100, high=100.4, low=97.0, close=99.0, time_ms=1000)
    apply_objective_price_tracking(setup, [candle], watermark_ms=0)
    setup.last_tracked_candle_ms = 1000
    apply_objective_price_tracking(setup, [candle], watermark_ms=1000)
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_SAME_CANDLE_AMBIGUOUS


@pytest.mark.asyncio
async def test_create_setup_finalizes_after_expiry_when_reconciled(session):
    created = datetime.now(timezone.utc) - timedelta(hours=3)
    expires = datetime.now(timezone.utc) - timedelta(minutes=15)
    structured = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
        "proposed_setup": {"new_setup": True, "thesis_fa": "test", "expires_hours": 48},
    }
    row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=structured)
    assert meta["created"]
    row.created_at = created
    row.expires_at = expires
    await session.commit()
    await session.refresh(row)
    from app.services.ai_setup_memory import _last_required_tracking_ms

    last_req = _last_required_tracking_ms(row)
    base_ms = int(created.timestamp() * 1000)
    c1 = Candle(open=100, high=100.5, low=99.5, close=100.2, time_ms=base_ms + 600_000)
    c2 = Candle(open=100, high=100.3, low=99.9, close=100.1, time_ms=last_req)
    bundle = KlineBundle(
        closed=[c1, c2],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)
    await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(row)
    assert row.objective_notes.get("validity_status") == VALIDITY_EXPIRED
    assert row.objective_notes.get("outcome_tracking_status") == TRACKING_COMPLETE
    assert row.status == "expired"
