from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.services.market_data import KlineBundle, MarketDataService
from app.services.monitor_kline_backfill import (
    MONITOR_PAGINATED_MAX_BARS,
    live_monitor_fetch_plan,
    replay_fetch_plan,
)
from app.services.monitor_kline_fetch import (
    fetch_monitor_5m_bundle,
    fetch_replay_5m_bundle,
    setup_history_covers_cursor,
)

pytestmark = pytest.mark.no_monitor_fetch_delegate

STEP = 300_000


def _candle(open_ms: int) -> Candle:
    return Candle(time_ms=open_ms, open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0)


def _series(from_ms: int, count: int) -> list[Candle]:
    return [_candle(from_ms + i * STEP) for i in range(count)]


@pytest.mark.asyncio
async def test_fresh_cursor_uses_recent_window_only():
    now_ms = 10_000 * STEP
    boundary = now_ms - 576 * STEP  # ~48h of 5m bars
    setup = SimpleNamespace(
        setup_id="fresh",
        created_at=datetime.fromtimestamp(boundary / 1000, tz=timezone.utc),
        monitor_from_ms=boundary,
        monitor_cursor_ms=now_ms - 3 * STEP,
    )
    plan = live_monitor_fetch_plan(
        setup_id="fresh",
        boundary_ms=boundary,
        cursor_ms=setup.monitor_cursor_ms,
        now_ms=now_ms,
    )
    assert plan.mode == "recent_window"
    assert plan.needs_pagination is False
    assert plan.from_ms > boundary - 50 * STEP

    recent = KlineBundle(
        closed=_series(now_ms - 200 * STEP, 200),
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = MarketDataService()
    market.fetch_kline_bundle = AsyncMock(return_value=recent)  # type: ignore[method-assign]
    market.fetch_historical_closed = AsyncMock()  # type: ignore[method-assign]

    bundle, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [setup], now_ms=now_ms)
    market.fetch_historical_closed.assert_not_awaited()
    assert meta["fetch_mode"] == "recent_window"
    covers, _ = setup_history_covers_cursor(plan=plan, closed_bars=bundle.closed, now_ms=now_ms)
    assert covers is True


@pytest.mark.asyncio
async def test_lag_800_bars_paginates_with_correct_max_bars():
    now_ms = 20_000 * STEP
    cursor = now_ms - 800 * STEP
    boundary = cursor - 10 * STEP
    setup = SimpleNamespace(
        setup_id="lag800",
        created_at=datetime.fromtimestamp(boundary / 1000, tz=timezone.utc),
        monitor_from_ms=boundary,
        monitor_cursor_ms=cursor,
    )
    plan = live_monitor_fetch_plan(
        setup_id="lag800",
        boundary_ms=boundary,
        cursor_ms=cursor,
        now_ms=now_ms,
    )
    assert plan.needs_pagination is True
    assert plan.max_bars == 800 + 48 + 15
    assert plan.max_bars <= MONITOR_PAGINATED_MAX_BARS

    hist = KlineBundle(
        closed=_series(plan.from_ms, plan.max_bars),
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    recent = KlineBundle(
        closed=_series(now_ms - 200 * STEP, 200),
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = MarketDataService()
    market.fetch_kline_bundle = AsyncMock(return_value=recent)  # type: ignore[method-assign]
    market.fetch_historical_closed = AsyncMock(return_value=hist)  # type: ignore[method-assign]

    await fetch_monitor_5m_bundle(market, "BTCUSDT", [setup], now_ms=now_ms)
    market.fetch_historical_closed.assert_awaited_once()
    _args, kwargs = market.fetch_historical_closed.await_args
    assert kwargs["max_bars"] == plan.max_bars


@pytest.mark.asyncio
async def test_lag_beyond_ceiling_flags_per_setup():
    now_ms = 30_000 * STEP
    cursor = now_ms - (MONITOR_PAGINATED_MAX_BARS + 100) * STEP
    setup = SimpleNamespace(
        setup_id="toolag",
        created_at=datetime.now(timezone.utc),
        monitor_from_ms=cursor - STEP,
        monitor_cursor_ms=cursor,
    )
    plan = live_monitor_fetch_plan(
        setup_id="toolag",
        boundary_ms=setup.monitor_from_ms,
        cursor_ms=cursor,
        now_ms=now_ms,
    )
    assert plan.exceeds_pagination_ceiling is True
    covers, reason = setup_history_covers_cursor(plan=plan, closed_bars=_series(now_ms - 200 * STEP, 200), now_ms=now_ms)
    assert covers is False
    assert reason == "lag_exceeds_pagination_ceiling"


@pytest.mark.asyncio
async def test_two_setups_isolated_one_bad_one_good():
    now_ms = 40_000 * STEP
    good_cursor = now_ms - 2 * STEP
    bad_cursor = now_ms - (MONITOR_PAGINATED_MAX_BARS + 50) * STEP
    good = SimpleNamespace(
        setup_id="good",
        created_at=datetime.now(timezone.utc),
        monitor_from_ms=good_cursor - 5 * STEP,
        monitor_cursor_ms=good_cursor,
    )
    bad = SimpleNamespace(
        setup_id="bad",
        created_at=datetime.now(timezone.utc),
        monitor_from_ms=bad_cursor - STEP,
        monitor_cursor_ms=bad_cursor,
    )
    good_plan = live_monitor_fetch_plan(
        setup_id="good",
        boundary_ms=good.monitor_from_ms,
        cursor_ms=good.monitor_cursor_ms,
        now_ms=now_ms,
    )
    bad_plan = live_monitor_fetch_plan(
        setup_id="bad",
        boundary_ms=bad.monitor_from_ms,
        cursor_ms=bad.monitor_cursor_ms,
        now_ms=now_ms,
    )

    merged = _series(good_plan.from_ms, 120)
    # dedupe by time
    by_t = {c.time_ms: c for c in merged}
    merged = sorted(by_t.values(), key=lambda c: c.time_ms)
    recent = KlineBundle(
        closed=merged,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = MarketDataService()
    market.fetch_kline_bundle = AsyncMock(return_value=recent)  # type: ignore[method-assign]
    market.fetch_historical_closed = AsyncMock(return_value=recent)  # type: ignore[method-assign]

    bundle, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [good, bad], now_ms=now_ms)
    assert meta["per_setup"][0]["setup_id"] == "good"
    good_ok, _ = setup_history_covers_cursor(plan=good_plan, closed_bars=bundle.closed, now_ms=now_ms)
    bad_ok, bad_reason = setup_history_covers_cursor(plan=bad_plan, closed_bars=bundle.closed, now_ms=now_ms)
    assert good_ok is True
    assert bad_ok is False
    assert bad_reason == "lag_exceeds_pagination_ceiling"


@pytest.mark.asyncio
async def test_replay_fetch_uses_boundary_not_cursor_anchor():
    now_ms = 50_000 * STEP
    boundary = now_ms - 400 * STEP
    setup = SimpleNamespace(
        setup_id="replay",
        created_at=datetime.fromtimestamp(boundary / 1000, tz=timezone.utc),
        monitor_from_ms=boundary,
        monitor_cursor_ms=now_ms - 5 * STEP,
    )
    plan = replay_fetch_plan(setup_id="replay", boundary_ms=boundary, now_ms=now_ms)
    assert plan.from_ms <= boundary
    assert plan.bars_needed >= 400

    hist = KlineBundle(
        closed=_series(plan.from_ms, min(plan.max_bars, 450)),
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = MarketDataService()
    market.fetch_historical_closed = AsyncMock(return_value=hist)  # type: ignore[method-assign]
    await fetch_replay_5m_bundle(market, "BTCUSDT", [setup], now_ms=now_ms)
    _args, kwargs = market.fetch_historical_closed.await_args
    assert kwargs["from_ms"] == plan.from_ms


@pytest.mark.asyncio
async def test_monitor_restart_no_duplicate_confirmation_cursor():
    from unittest.mock import patch

    from app.db import SessionLocal, init_db
    from app.domain.setup_lifecycle import WAIT_LONG
    from app.models import AiProposedSetup
    from app.services.opportunity_monitor import OpportunityMonitorService

    await init_db()
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    boundary = now_ms - 10 * STEP
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="restart-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=boundary + 4 * STEP,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=12),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {"entry_zone_touch": boundary + 2 * STEP},
            },
            structured_snapshot={},
        )
        session.add(setup)
        await session.commit()
        cursor_before = setup.monitor_cursor_ms
        ts_before = setup.confirmation_state["confirmation_timestamps"].copy()

        closed = _series(boundary, 12)
        bundle = KlineBundle(
            closed=closed,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        mon = OpportunityMonitorService()
        mon.market.fetch_kline_bundle = AsyncMock(return_value=bundle)  # type: ignore[method-assign]
        mon.market.fetch_historical_closed = AsyncMock(return_value=bundle)  # type: ignore[method-assign]
        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
            await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.refresh(setup)
        assert setup.confirmation_state["confirmation_timestamps"] == ts_before
        assert setup.monitor_cursor_ms == cursor_before
