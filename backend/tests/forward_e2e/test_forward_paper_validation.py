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
from app.domain.setup_lifecycle import REASON_MISSING_MARKET_DATA
from tests.forward_e2e.harness import (
    INTERVAL_1M,
    INTERVAL_5M,
    ICT_SEQUENCE,
    ScenarioTimeline,
    ScriptedMarket,
    align_minute_open_ms,
    assert_net_rr_after_costs,
    bar_1m,
    bar_5m,
    build_lagged_backfill_catalog,
    closed_only_before,
    count_paper_orders,
    emit_scenario_evidence,
    enable_auto_paper,
    enrich_timeline_from_setup,
    fill_pending_limit_order,
    finalize_success_pipeline_timeline,
    ict_confirmation_bars,
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
    expected_bars = ict_confirmation_bars(boundary, "LONG")
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
    market.ticker_price = 100.25
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()

    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert summary["promoted"] == 1
    assert summary["orders"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    assert await count_paper_orders(session) == 1
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    order_ms = int(pos.monitor_from_ms)
    assert pos.status == "pending_entry"
    assert pos.order_type == "limit"

    fill = await fill_pending_limit_order(session, market, setup_id=setup_id, closed_5m=closed_5m)
    assert fill.fill_bar_open_ms > order_ms
    assert fill.monitor_eval_now_ms > fill.fill_bar_close_ms
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    net_rr = await assert_net_rr_after_costs(
        session,
        direction=setup.direction,
        entry=float(pos.fill_price or pos.entry_price),
        sl=float(setup.stop_loss),
        tp=float(setup.take_profit),
    )
    finalize_success_pipeline_timeline(
        timeline,
        setup=setup,
        pos=pos,
        expected_bars=expected_bars,
        monitor_eval_ms=now_ms,
        fill=fill,
        order_ms=order_ms,
        net_rr=net_rr,
    )
    emit_scenario_evidence(timeline)


@pytest.mark.asyncio
async def test_success_short_full_pipeline(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    expected_bars = ict_confirmation_bars(boundary, "SHORT")
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
    market.ticker_price = 99.5
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert summary["promoted"] == 1
    assert summary["orders"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    assert await count_paper_orders(session) == 1
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    order_ms = int(pos.monitor_from_ms)
    assert pos.status == "pending_entry"
    assert pos.order_type == "limit"

    fill = await fill_pending_limit_order(session, market, setup_id=setup_id, closed_5m=closed_5m)
    assert fill.fill_bar_open_ms > order_ms
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    net_rr = await assert_net_rr_after_costs(
        session,
        direction=setup.direction,
        entry=float(pos.fill_price or pos.entry_price),
        sl=float(setup.stop_loss),
        tp=float(setup.take_profit),
    )
    finalize_success_pipeline_timeline(
        timeline,
        setup=setup,
        pos=pos,
        expected_bars=expected_bars,
        monitor_eval_ms=now_ms,
        fill=fill,
        order_ms=order_ms,
        net_rr=net_rr,
    )
    emit_scenario_evidence(timeline)


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
async def test_backfill_recovery_full_context_promotes(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    catalog = build_lagged_backfill_catalog(boundary, pad_bars_before_ict=120)
    now_ms = promotion_now_ms(catalog)
    initial_cursor = catalog[0].time_ms - INTERVAL_5M
    timeline = ScenarioTimeline("backfill_success", "complete structural context + ICT tail promotes")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=110, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = initial_cursor
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.catalog_5m = catalog
    market.closed_5m = catalog[-120:]
    from app.services.monitor_kline_backfill import live_monitor_fetch_plan
    from app.services.monitor_kline_fetch import fetch_monitor_5m_bundle, setup_history_covers_cursor

    bundle, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [row], now_ms=now_ms)
    plan = live_monitor_fetch_plan(
        setup_id=row.setup_id,
        boundary_ms=boundary,
        cursor_ms=initial_cursor,
        now_ms=now_ms,
    )
    covers, cover_reason = setup_history_covers_cursor(plan=plan, closed_bars=bundle.closed, now_ms=now_ms)
    assert covers, cover_reason
    assert meta.get("deep_fetch_complete") is True
    assert market.historical_fetch_count >= 1
    per_setup = (meta.get("per_setup") or [{}])[0]
    assert per_setup.get("mode") in ("extended_window", "paginated")

    market.closed_5m = bundle.closed
    market.catalog_5m = catalog
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert setup.last_rejection_reason != REASON_MISSING_MARKET_DATA
    assert summary["promoted"] == 1
    assert summary["orders"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    assert setup.monitor_cursor_ms is not None and setup.monitor_cursor_ms > initial_cursor
    assert (setup.confirmation_state or {}).get("completed") == list(ICT_SEQUENCE)
    timeline.add("backfill", detail=f"historical_fetches={market.historical_fetch_count} merged={meta.get('merged_bar_count')}")
    timeline.add("lifecycle", detail=setup.lifecycle_state)
    _finalize(timeline, setup)


@pytest.mark.asyncio
@pytest.mark.no_monitor_fetch_delegate
async def test_backfill_incomplete_history_fail_closed(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    catalog = build_lagged_backfill_catalog(boundary, pad_bars_before_ict=120)
    gap_ms = boundary + INTERVAL_5M
    assert any(c.time_ms == gap_ms for c in catalog)
    incomplete = [c for c in catalog if c.time_ms != gap_ms]
    now_ms = promotion_now_ms(catalog)
    initial_cursor = boundary - 1
    timeline = ScenarioTimeline("backfill_incomplete", "missing bar in required span suspends monitor")

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=111, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = initial_cursor
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.catalog_5m = incomplete
    market.closed_5m = incomplete[-120:]
    from app.services.monitor_kline_backfill import live_monitor_fetch_plan
    from app.services.monitor_kline_fetch import fetch_monitor_5m_bundle, setup_history_covers_cursor

    bundle, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [row], now_ms=now_ms)
    plan = live_monitor_fetch_plan(
        setup_id=row.setup_id,
        boundary_ms=boundary,
        cursor_ms=initial_cursor,
        now_ms=now_ms,
    )
    covers, cover_reason = setup_history_covers_cursor(plan=plan, closed_bars=bundle.closed, now_ms=now_ms)
    assert not covers, cover_reason

    market.closed_5m = bundle.closed
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert summary["promoted"] == 0
    assert summary["orders"] == 0
    assert setup.lifecycle_state == WAIT_LONG
    assert setup.last_rejection_reason == REASON_MISSING_MARKET_DATA
    assert setup.monitor_cursor_ms == initial_cursor
    assert await count_paper_orders(session) == 0
    timeline.add("blocked", detail=f"cover_reason={cover_reason} gap_ms={gap_ms}")
    _finalize(timeline, setup)
