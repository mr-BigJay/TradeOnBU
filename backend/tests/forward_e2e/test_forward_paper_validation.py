"""
Forward E2E: Conditional setup → 5m ICT confirmations → READY → paper limit → 1m fill.

Isolated DB: backend/data/test_forward_paper_e2e.db
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.domain.candle_utils import candle_close_ms
from app.domain.setup_lifecycle import (
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_OPEN,
    WAIT_LONG,
)
from app.models import AiProposedSetup, SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from tests.forward_e2e.conftest import ForwardSession
from tests.forward_e2e.harness import (
    INTERVAL_1M,
    INTERVAL_5M,
    ScenarioTimeline,
    ScriptedMarket,
    align_minute_open_ms,
    bar_1m,
    bar_5m,
    closed_only_before,
    count_paper_orders,
    emit_scenario_evidence,
    enable_auto_paper,
    enrich_timeline_from_setup,
    fill_pending_limit_order,
    long_closed_through_mss_with_forming_touch,
    ms_to_dt,
    production_conditional_wait_short,
    promotion_now_ms,
    run_monitor_fresh_session,
    run_opportunity_monitor,
    run_position_monitor,
    setup_boundary,
    sweep_mss_poi_long_bars,
    sweep_mss_poi_short_bars,
)
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long

pytestmark = pytest.mark.forward_e2e

CREATED = datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc)


def _finalize(timeline: ScenarioTimeline, setup: AiProposedSetup | None = None) -> None:
    if setup is not None:
        enrich_timeline_from_setup(timeline, setup)
    emit_scenario_evidence(timeline)


@pytest.mark.asyncio
async def test_success_long_full_pipeline(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("success_long", "valid ICT sequence and net R:R ≥ 2")

    await enable_auto_paper(session)
    row, meta = await create_setup_from_structured(
        session, run_id=100, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    assert meta["created"]
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    market.catalog_5m = closed_5m
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()

    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert summary["promoted"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    ts = setup.confirmation_state.get("confirmation_timestamps") or {}
    timeline.add("ready", at_ms=now_ms, detail=setup.lifecycle_state)
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    timeline.add("paper_limit_order", at_ms=pos.monitor_from_ms, detail=str(pos.limit_entry_price))

    fill_at = await fill_pending_limit_order(session, market, setup_id=setup_id, closed_5m=closed_5m)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    assert pos.status == "open"
    assert pos.monitor_from_ms < fill_at
    assert setup.lifecycle_state == POSITION_OPEN
    timeline.add("fill", at_ms=fill_at, detail=str(pos.fill_price))
    timeline.delays_ms["confirm_to_order_ms"] = int(pos.monitor_from_ms - ts["entry_zone_touch"])
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_success_short_full_pipeline(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_short_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("success_short", "SHORT ICT + R:R ≥ 2")

    await enable_auto_paper(session)
    row, meta = await create_setup_from_structured(
        session, run_id=101, symbol="BTCUSDT", structured=production_conditional_wait_short()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    market.catalog_5m = closed_5m
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    await fill_pending_limit_order(session, market, setup_id=setup_id, closed_5m=closed_5m)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert setup.lifecycle_state == POSITION_OPEN
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_open_5m_candle_does_not_promote(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed, forming, now_ms = long_closed_through_mss_with_forming_touch(boundary)
    timeline = ScenarioTimeline("open_5m_forming", "touch only on open 5m bar")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=102, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed
    market.forming_5m = forming
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    ts = setup.confirmation_state.get("confirmation_timestamps") or {}
    assert "liquidity_sweep" in ts and "mss" in ts
    assert "entry_zone_touch" not in ts
    assert setup.lifecycle_state == WAIT_LONG
    assert summary["promoted"] == 0
    timeline.add("blocked", detail="forming_5m_entry_touch_not_closed")
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_incomplete_confirmation_stays_wait(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)[:2]
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("incomplete_confirmation", "sequence not finished")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=103, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert setup.lifecycle_state == WAIT_LONG
    assert setup.confirmation_state.get("missing")
    timeline.add("no_entry", detail=setup.last_rejection_reason or "missing_confirmations")
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_net_rr_below_min_blocks_promotion(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("net_rr_below_min", "net R:R < 2 blocks paper order")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=104, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    row.stop_loss = 98.0
    row.take_profit = 103.0
    row.entry_price = 100.0
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert summary["orders"] == 0
    timeline.add("blocked", detail=setup.lifecycle_state)
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_stale_market_data_blocks_promotion(forward_session):
    from unittest.mock import patch

    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("stale_market_data", "freshness gate blocked promotion")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=105, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed_5m
    from app.services.opportunity_monitor import OpportunityMonitorService

    mon = OpportunityMonitorService()
    mon.market = market  # type: ignore[assignment]
    with patch("app.services.opportunity_monitor._utc_now", return_value=ms_to_dt(now_ms)):
        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(False, 999, 600)):
            summary = await mon.monitor_setups(session, symbol="BTCUSDT")
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert summary["promoted"] == 0
    timeline.add("blocked", detail="stale_market_data")
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_incomplete_5m_bundle_blocks_promotion(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("5m_gap", "missing 5m bar triggered gap suspend")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=106, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = [closed_5m[0], closed_5m[2], closed_5m[3]]
    cursor_before = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one().monitor_cursor_ms
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert summary["orders"] == 0
    assert setup.monitor_cursor_ms == cursor_before
    timeline.add("blocked", detail="gap_suspend")
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_structural_invalidation(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    bars = sweep_mss_poi_long_bars(boundary)
    inv = 93.0
    bars.append(bar_5m(boundary + 3 * INTERVAL_5M, o=94.0, h=94.5, l=92.0, c=92.5))
    now_ms = promotion_now_ms(bars)
    timeline = ScenarioTimeline("structural_invalidation", "invalidation before entry")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=107, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    row.invalidation_level = inv
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, bars, "5m")
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert setup.lifecycle_state == INVALIDATED
    timeline.add("terminal", detail=INVALIDATED)
    _finalize(timeline, setup)


@pytest.mark.asyncio
async def test_limit_order_never_fills(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("limit_no_fill", "closed 1m did not trade through limit")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=108, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    entry = float(pos.limit_entry_price or 100.0)
    order_ms = int(pos.monitor_from_ms)
    timeline.add("paper_limit_order", at_ms=order_ms, detail=str(entry))
    miss_open = align_minute_open_ms(order_ms + INTERVAL_1M)
    away = bar_1m(miss_open, o=entry + 5, h=entry + 6, l=entry + 4, c=entry + 5)
    assert not (away.low <= entry <= away.high)
    market.closed_1m = [away]
    market.catalog_1m = [away]
    market.closed_5m = closed_5m
    eval_now = candle_close_ms(away, "1m") + 5_000
    assert eval_now > order_ms
    await run_position_monitor(session, market, now_ms=eval_now)
    await session.commit()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    assert pos.status == "pending_entry"
    timeline.add("no_fill", at_ms=eval_now, detail="price away from limit")
    _finalize(timeline)


@pytest.mark.asyncio
async def test_monitor_restart_preserves_cursor_no_duplicate_orders(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)
    timeline = ScenarioTimeline("monitor_restart", "fresh session + monitor, single order")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=109, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = closed_only_before(now_ms, closed_5m, "5m")
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()

    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    cursor_after = setup.monitor_cursor_ms

    for _ in range(2):
        await run_monitor_fresh_session(ForwardSession, market, now_ms=now_ms)
    async with ForwardSession() as count_session:
        assert await count_paper_orders(count_session) == 1
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert setup.monitor_cursor_ms == cursor_after
    timeline.add("orders", detail="count=1 after restarts")
    _finalize(timeline, setup)


@pytest.mark.asyncio
@pytest.mark.no_monitor_fetch_delegate
async def test_backfill_via_real_fetch_monitor_5m_bundle(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    lag_bars = 180
    tail = sweep_mss_poi_long_bars(boundary + (lag_bars - 3) * INTERVAL_5M)
    catalog: list = []
    start = boundary
    for i in range(lag_bars - 3):
        catalog.append(bar_5m(start + i * INTERVAL_5M, o=100.0, h=100.2, l=99.8, c=100.1))
    catalog.extend(tail[-3:])
    now_ms = promotion_now_ms(catalog)
    timeline = ScenarioTimeline("backfill_extended_window", "real fetch_monitor_5m_bundle with historical merge")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=110, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.catalog_5m = catalog
    market.closed_5m = catalog[-120:]
    from app.services.monitor_kline_fetch import fetch_monitor_5m_bundle

    bundle, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [row], now_ms=now_ms)
    per_setup = (meta.get("per_setup") or [{}])[0]
    assert per_setup.get("mode") in ("extended_window", "paginated")
    assert market.historical_fetch_count >= 1
    market.closed_5m = bundle.closed
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert setup.monitor_cursor_ms is not None
    assert setup.monitor_cursor_ms is not None and setup.monitor_cursor_ms >= boundary - INTERVAL_5M
    timeline.add("backfill", detail=f"historical_fetches={market.historical_fetch_count}")
    timeline.add("monitor", detail=str(summary))
    _finalize(timeline, setup)
