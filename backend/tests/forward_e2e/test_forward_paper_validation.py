"""
Forward E2E: Conditional setup → 5m ICT confirmations → READY → paper limit → 1m fill.

Uses real OpportunityMonitorService, ict_confirmation_engine (via monitor), SimPositionMonitorService.
Market time and klines are scripted; isolated DB: tests/data/test_forward_paper_e2e.db
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.domain.candle_utils import candle_close_ms
from app.domain.setup_lifecycle import (
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_OPEN,
    READY_LONG,
    READY_SHORT,
    WAIT_LONG,
    WAIT_SHORT,
)
from app.models import AiProposedSetup, SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from tests.forward_e2e.harness import (
    INTERVAL_1M,
    INTERVAL_5M,
    ScenarioTimeline,
    ScriptedMarket,
    bar_1m,
    bar_5m,
    count_paper_orders,
    enable_auto_paper,
    enrich_timeline_from_setup,
    ms_to_dt,
    production_conditional_wait_short,
    promotion_now_ms,
    run_opportunity_monitor,
    run_position_monitor,
    setup_boundary,
    sweep_mss_poi_long_bars,
    sweep_mss_poi_short_bars,
)
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long

pytestmark = pytest.mark.forward_e2e

CREATED = datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc)


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
    market.closed_5m = closed_5m
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()

    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    enrich_timeline_from_setup(timeline, setup)
    assert summary["promoted"] == 1
    assert summary["orders"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    assert (setup.confirmation_state or {}).get("completed") == [
        "liquidity_sweep",
        "mss",
        "entry_zone_touch",
    ]
    ts = setup.confirmation_state.get("confirmation_timestamps") or {}
    assert ts["liquidity_sweep"] < ts["mss"] < ts["entry_zone_touch"]
    timeline.add("ready_implied", at_ms=now_ms, detail="promoted_to_pending")
    assert await count_paper_orders(session) == 1

    summary2 = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    assert summary2.get("orders", 0) == 0
    assert await count_paper_orders(session) == 1

    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    order_ms = pos.monitor_from_ms
    entry = float(pos.limit_entry_price or setup.entry_price)
    timeline.add("paper_limit_order", at_ms=order_ms, detail=f"limit@{entry}")

    from tests.forward_e2e.harness import fill_pending_limit_order

    fill_at = await fill_pending_limit_order(
        session, market, setup_id=setup_id, closed_5m=closed_5m
    )
    await session.commit()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert pos.status == "open"
    assert setup.lifecycle_state == POSITION_OPEN
    timeline.add("fill", at_ms=fill_at, detail=str(pos.fill_price))
    timeline.delays_ms["confirm_to_order_ms"] = (order_ms - ts["entry_zone_touch"]) if order_ms else None
    report = timeline.to_report()
    assert report["scenario_id"] == "success_long"


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
    assert meta["created"]
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()
    setup_id = row.setup_id

    market = ScriptedMarket()
    market.closed_5m = closed_5m
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    enrich_timeline_from_setup(timeline, setup)
    assert summary["promoted"] == 1
    assert setup.lifecycle_state == PENDING_ENTRY
    seq = (setup.confirmation_state or {}).get("sequence") or []
    assert "liquidity_sweep" in seq
    from tests.forward_e2e.harness import fill_pending_limit_order

    await fill_pending_limit_order(session, market, setup_id=setup_id, closed_5m=closed_5m)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))).scalar_one()
    assert setup.lifecycle_state == POSITION_OPEN


@pytest.mark.asyncio
async def test_open_5m_candle_does_not_promote(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    partial = sweep_mss_poi_long_bars(boundary)[:2]
    forming = sweep_mss_poi_long_bars(boundary)[2]
    now_ms = candle_close_ms(partial[-1], "5m") + 5_000

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=102, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = partial
    market.forming_5m = forming
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert setup.lifecycle_state in (WAIT_LONG, "WAIT_LONG")
    assert summary["promoted"] == 0
    assert await count_paper_orders(session) == 0


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
    market.closed_5m = closed_5m
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    enrich_timeline_from_setup(timeline, setup)
    assert setup.lifecycle_state == WAIT_LONG
    missing = setup.confirmation_state.get("missing") or []
    assert missing
    assert await count_paper_orders(session) == 0


@pytest.mark.asyncio
async def test_net_rr_below_min_blocks_promotion(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)

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
    market.closed_5m = closed_5m
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert summary["promoted"] == 0
    assert summary["orders"] == 0
    assert setup.lifecycle_state in (WAIT_LONG, "ICT_CONFIRMED_LONG")


@pytest.mark.asyncio
async def test_stale_market_data_blocks_promotion(forward_session):
    from unittest.mock import patch

    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)

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
    mon = __import__("app.services.opportunity_monitor", fromlist=["OpportunityMonitorService"]).OpportunityMonitorService()
    mon.market = market
    with patch("app.services.opportunity_monitor._utc_now", return_value=ms_to_dt(now_ms)):
        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(False, 999, 600)):
            summary = await mon.monitor_setups(session, symbol="BTCUSDT")
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert summary["promoted"] == 0
    assert setup.lifecycle_state == WAIT_LONG


@pytest.mark.asyncio
async def test_incomplete_5m_bundle_blocks_promotion(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=106, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = [closed_5m[0], closed_5m[2], closed_5m[3]]
    cursor_before = row.monitor_cursor_ms
    summary = await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert summary["orders"] == 0
    assert setup.lifecycle_state == WAIT_LONG
    assert setup.monitor_cursor_ms == cursor_before


@pytest.mark.asyncio
async def test_structural_invalidation(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    bars = sweep_mss_poi_long_bars(boundary)
    inv = 93.0
    bars.append(bar_5m(boundary + 3 * INTERVAL_5M, o=94.0, h=94.5, l=92.0, c=92.5))
    now_ms = promotion_now_ms(bars)

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
    market.closed_5m = bars
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert setup.lifecycle_state == INVALIDATED
    assert await count_paper_orders(session) == 0


@pytest.mark.asyncio
async def test_limit_order_never_fills(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)

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
    market.closed_5m = closed_5m
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    entry = float(pos.limit_entry_price or 100.0)
    order_ms = pos.monitor_from_ms
    away = bar_1m(order_ms + INTERVAL_1M, o=entry + 5, h=entry + 6, l=entry + 4, c=entry + 5)
    market.closed_1m = [away]
    await run_position_monitor(session, market, now_ms=order_ms + INTERVAL_1M + 30_000)
    await session.commit()
    pos = (await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))).scalar_one()
    assert pos.status == "pending_entry"


@pytest.mark.asyncio
async def test_monitor_restart_preserves_cursor_no_duplicate_orders(forward_session):
    session = forward_session
    boundary = setup_boundary(CREATED)
    closed_5m = sweep_mss_poi_long_bars(boundary)
    now_ms = promotion_now_ms(closed_5m)

    await enable_auto_paper(session)
    row, _ = await create_setup_from_structured(
        session, run_id=109, symbol="BTCUSDT", structured=_production_conditional_wait_long()
    )
    row.created_at = CREATED
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    await session.commit()

    market = ScriptedMarket()
    market.closed_5m = closed_5m
    await run_opportunity_monitor(session, market, now_ms=now_ms)
    await session.commit()
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    cursor_after = setup.monitor_cursor_ms
    assert cursor_after is not None

    for _ in range(3):
        await run_opportunity_monitor(session, market, now_ms=now_ms)
        await session.commit()
    assert await count_paper_orders(session) == 1
    setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))).scalar_one()
    assert setup.monitor_cursor_ms == cursor_after
