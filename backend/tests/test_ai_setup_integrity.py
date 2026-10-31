"""Data-integrity regressions: contiguous gaps, expiry reconciliation, immutable forecasts."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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
    build_ai_memory_context,
    create_setup_from_structured,
    refresh_setup_objective_state,
    serialize_setup_row,
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
        lifecycle_state="WAIT_LONG",
        thesis_fa="original thesis",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    from tests.conftest import with_immediate_monitor_fields

    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 106.0,
            "proposed_setup": {"continue_setup_id": "orig", "thesis_fa": "revised thesis"},
        }
    )
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
    from tests.conftest import with_immediate_monitor_fields

    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True, "thesis_fa": "first"},
        }
    )
    r1, _ = await create_setup_from_structured(session, run_id=5, symbol="BTCUSDT", structured=structured)
    await session.commit()
    structured2 = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True, "thesis_fa": "second"},
        }
    )
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


def test_terminal_tp_not_overwritten_by_later_invalidation():
    setup = _long_setup(
        entry_touched=True,
        invalidation_level=99.0,
        objective_notes={"lifecycle_phase": "entry_touched"},
    )
    apply_objective_price_tracking(
        setup,
        [
            Candle(open=104, high=106, low=103, close=105, time_ms=1000),
            Candle(open=98, high=98.5, low=97.5, close=97.8, time_ms=2000),
        ],
        watermark_ms=0,
    )
    assert setup.objective_notes["lifecycle_phase"] == LIFECYCLE_TP_AFTER_ENTRY
    assert setup.status == "open"


def test_expiry_straddle_does_not_count_touch():
    exp = datetime(2026, 6, 15, 12, 7, 30, tzinfo=timezone.utc)
    exp_ms = int(exp.timestamp() * 1000)
    step = 300_000
    open_ms = exp_ms - 120_000
    setup = _long_setup(
        expires_at=exp,
        created_at=exp - timedelta(hours=1),
        objective_notes={},
    )
    straddle = Candle(open=100, high=101, low=99.5, close=100.5, time_ms=open_ms)
    assert straddle.time_ms < exp_ms < open_ms + step
    apply_objective_price_tracking(setup, [straddle], watermark_ms=0)
    assert not setup.entry_touched
    assert setup.objective_notes.get("expiry_boundary_uncertain") is True


def test_fully_closed_bar_before_non_aligned_expiry_counts():
    exp = datetime(2026, 6, 15, 12, 7, 30, tzinfo=timezone.utc)
    exp_ms = int(exp.timestamp() * 1000)
    step = 300_000
    open_ms = ((exp_ms - step) // step) * step
    while open_ms + step > exp_ms:
        open_ms -= step
    setup = _long_setup(
        expires_at=exp,
        created_at=exp - timedelta(hours=1),
        entry_zone_low=99.0,
        entry_zone_high=101.0,
    )
    eligible = Candle(open=100, high=100.5, low=99.6, close=100.2, time_ms=open_ms)
    assert open_ms + step <= exp_ms
    apply_objective_price_tracking(setup, [eligible], watermark_ms=0)
    assert setup.entry_touched is True
    assert not setup.objective_notes.get("expiry_boundary_uncertain")


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
    from tests.conftest import with_immediate_monitor_fields

    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True, "thesis_fa": "test", "expires_hours": 48},
        }
    )
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


@pytest.mark.asyncio
async def test_refresh_persists_expiry_straddle_uncertainty_in_db_and_memory(session):
    """Integration: straddling 12:05–12:10 bar with expiry 12:07:30 must not count touches."""
    exp = datetime(2026, 6, 15, 12, 7, 30, tzinfo=timezone.utc)
    created = datetime(2026, 6, 15, 11, 0, 0, tzinfo=timezone.utc)
    fixed_now = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
    open_ms = int(datetime(2026, 6, 15, 12, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    exp_ms = int(exp.timestamp() * 1000)
    step = 300_000

    setup = AiProposedSetup(
        setup_id="exp-straddle-refresh",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=98.0,
        take_profit=105.0,
        created_at=created,
        expires_at=exp,
        last_tracked_candle_ms=0,
    )
    session.add(setup)
    await session.commit()

    straddle = Candle(open=100, high=102, low=98, close=101, time_ms=open_ms)
    assert open_ms < exp_ms < open_ms + step

    bundle = KlineBundle(
        closed=[straddle],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)

    with patch("app.services.ai_setup_memory._utc", return_value=fixed_now):
        await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)

    await session.commit()
    await session.refresh(setup)
    assert setup.entry_touched is False
    assert setup.sl_touched is False
    assert setup.tp_touched is False
    assert setup.objective_notes.get("expiry_boundary_uncertain") is True
    assert open_ms in (setup.objective_notes.get("expiry_boundary_straddle_candles_ms") or [])
    assert setup.last_tracked_candle_ms == open_ms
    first_fetch_from_ms = market.fetch_historical_closed.call_args_list[0][0][2]
    assert first_fetch_from_ms <= open_ms

    empty_bundle = KlineBundle(
        closed=[],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market.fetch_historical_closed = AsyncMock(return_value=empty_bundle)
    with patch("app.services.ai_setup_memory._utc", return_value=fixed_now):
        memory = await build_ai_memory_context(
            session, symbol="BTCUSDT", candles_by_interval={}, market=market
        )

    serialized = serialize_setup_row(setup)
    assert serialized["objective_notes"].get("expiry_boundary_uncertain") is True

    all_setups = (
        memory["open_setups"]
        + memory.get("reconciling_expired_setups", [])
        + memory.get("recent_closed_setups", [])
    )
    mem_row = next(s for s in all_setups if s["setup_id"] == setup.setup_id)
    assert mem_row["objective_notes"].get("expiry_boundary_uncertain") is True
    assert open_ms in (mem_row["objective_notes"].get("expiry_boundary_straddle_candles_ms") or [])
    assert mem_row["entry_touched"] is False

    setup.status = "open"
    setup.last_tracked_candle_ms = open_ms
    await session.commit()
    market.fetch_historical_closed.reset_mock()
    market.fetch_historical_closed = AsyncMock(return_value=empty_bundle)
    with patch("app.services.ai_setup_memory._utc", return_value=fixed_now):
        await refresh_setup_objective_state(session, symbol="BTCUSDT", candles_by_interval={}, market=market)
    await session.refresh(setup)
    assert setup.entry_touched is False
    assert setup.objective_notes.get("expiry_boundary_uncertain") is True
    if market.fetch_historical_closed.called:
        assert market.fetch_historical_closed.call_args[0][2] == open_ms + step
