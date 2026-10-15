"""AI WAIT plans: chronological proof, risk checks, opt-in and no phantom fills."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.domain.candles import Candle
from app.models import AiConditionalPlan, SimPosition
from app.services.ai_conditional_plans import monitor_conditional_plans, persist_conditional_plan
from app.services.conditional_ict import STEP_MS, watch_closed_bars
from app.services.market_data import KlineBundle
from app.services.position.settings_service import get_or_create_settings
from app.services.position import wallet_service as wallet


def candle(t: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=t, open=o, high=h, low=l, close=c, volume=5.0)


def sequence(t: int) -> list[Candle]:
    history = [
        candle(t - (8 - i) * STEP_MS, o=100.0, h=101.0, l=99.8, c=100.2)
        for i in range(8)
    ]
    history.append(candle(t, o=99.2, h=101.0, l=98.2, c=100.6))  # sellside sweep + POI reclaim
    history.append(candle(t + STEP_MS, o=100.8, h=104.2, l=100.7, c=103.9))  # BOS + displacement
    return history


def scan(bars: list[Candle], *, start: int, cursor: int = 0, evidence=None, now: int | None = None):
    return watch_closed_bars(
        bars, direction="LONG", zone_low=98.8, zone_high=99.4,
        stop_loss=97, take_profit=110, start_ms=start, cursor_ms=cursor,
        evidence=evidence, now_ms=now or (start + 3 * STEP_MS),
        expires_ms=start + 10 * STEP_MS,
    )


def test_wait_requires_post_plan_sweep_poi_and_later_displacement():
    t = 100 * STEP_MS
    result = scan(sequence(t), start=t)
    assert result.state == "ready"
    assert result.reason == "structure_confirmed"
    assert result.evidence["sweep_ms"] == t
    assert result.evidence["poi_ms"] == t
    assert result.evidence["structure_ms"] == t + STEP_MS


def test_no_backdating_from_pre_plan_sweep():
    t = 100 * STEP_MS
    bars = sequence(t)
    # AI plan is created after sweep: the historical sweep may be a price
    # reference but can never be borrowed as a post-plan event.
    result = scan(bars, start=t + STEP_MS, now=t + 3 * STEP_MS)
    assert result.state == "watching"
    assert result.evidence.get("sweep_ms") is None


def test_missing_closed_bar_suspends_instead_of_confirming():
    t = 100 * STEP_MS
    bars = [c for c in sequence(t) if c.time_ms != t]
    result = scan(bars, start=t)
    assert result.state == "watching"
    assert result.cursor_ms == 0
    assert result.reason == "history_gap"


def test_invalidation_before_confirmation_is_terminal():
    t = 100 * STEP_MS
    bars = sequence(t)
    bars[8] = candle(t, o=99, h=101, l=96, c=100.6)
    result = scan(bars, start=t)
    assert result.state == "invalidated"
    assert result.reason == "sl_or_target_touched_before_order"


def test_in_progress_candle_is_not_eligible():
    t = 100 * STEP_MS
    result = scan(sequence(t), start=t, now=t + STEP_MS - 1)
    assert result.state == "watching"
    assert result.cursor_ms == 0


def wait_json() -> dict:
    return {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 98.8,
        "conditional_entry_zone_high": 99.4,
        "conditional_entry_price": 99.05,
        "stop_loss": 97.0,
        "take_profit": 110.0,
        "confirmation_required": "liquidity sweep, POI reaction, displacement and directional BOS",
        "proposed_setup": {"expires_hours": 8},
    }


@pytest.mark.asyncio
async def test_persist_wait_and_dedupe_but_not_flat_no_trade(session):
    flat = dict(wait_json(), final_ai_decision="NO_TRADE", setup_mode="flat")
    skipped = await persist_conditional_plan(session, run_id=1, symbol="BTCUSDT", structured=flat)
    assert skipped["created"] is False
    first = await persist_conditional_plan(session, run_id=1, symbol="BTCUSDT", structured=wait_json())
    again = await persist_conditional_plan(session, run_id=2, symbol="BTCUSDT", structured=wait_json())
    assert first["created"] is True
    assert again["reason"] == "duplicate_active_plan"
    assert first["plan_id"] == again["plan_id"]
    n = await session.scalar(select(func.count()).select_from(AiConditionalPlan))
    assert n == 1


@pytest.mark.asyncio
async def test_wait_without_numeric_levels_is_not_executable(session):
    idea = wait_json()
    idea.pop("take_profit")
    result = await persist_conditional_plan(session, run_id=3, symbol="BTCUSDT", structured=idea)
    assert result["skipped"] is True
    assert result["reason"] == "missing_or_outside_plan_levels"


@pytest.mark.asyncio
async def test_worker_creates_exactly_one_paper_limit_after_proof_and_opt_in(session):
    t = (int(datetime.now(timezone.utc).timestamp() * 1000) // STEP_MS - 3) * STEP_MS
    watch = AiConditionalPlan(
        source_run_id=8, symbol="BTCUSDT", direction="LONG", status="watching",
        zone_low=98.8, zone_high=99.4, entry_price=99.05,
        stop_loss=97.0, take_profit=110.0,
        created_at=datetime.fromtimestamp((t - STEP_MS + 1000) / 1000, tz=timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        evidence={}, last_checked_candle_ms=0,
    )
    session.add(watch)
    await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000)
    paper = await get_or_create_settings(session)
    paper.auto_open_paper_positions = True
    paper.initial_balance_set = True
    await session.flush()

    bundle = KlineBundle(
        closed=sequence(t), forming=None, gaps_detected=[], gaps_remaining=[],
        data_complete=True, interval="5m",
    )
    market = SimpleNamespace(
        fetch_historical_closed=AsyncMock(return_value=bundle),
        fetch_live_price=AsyncMock(return_value=SimpleNamespace(price=105.0)),
        is_quote_stale=lambda quote: False,
    )
    outcome = await monitor_conditional_plans(session, market)
    assert outcome and outcome[0]["status"] == "activated"
    assert watch.linked_setup_id is not None
    pos = (await session.execute(select(SimPosition))).scalar_one()
    assert pos.status == "pending_entry"
    assert pos.order_type == "limit"
    assert pos.limit_entry_price == 99.05
    assert pos.extra["ict_entry_snapshot"]["watch_provenance"]["conditional_plan_id"] == watch.id
    await session.commit()

    await monitor_conditional_plans(session, market)
    n = await session.scalar(select(func.count()).select_from(SimPosition))
    assert n == 1


@pytest.mark.asyncio
async def test_wait_ready_but_auto_disabled_creates_no_paper_position(session):
    t = (int(datetime.now(timezone.utc).timestamp() * 1000) // STEP_MS - 3) * STEP_MS
    watch = AiConditionalPlan(
        source_run_id=8, symbol="BTCUSDT", direction="LONG", status="watching",
        zone_low=98.8, zone_high=99.4, entry_price=99.05, stop_loss=97, take_profit=110,
        created_at=datetime.fromtimestamp((t - STEP_MS + 1000) / 1000, tz=timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8), evidence={},
    )
    session.add(watch)
    bundle = KlineBundle(
        closed=sequence(t), forming=None, gaps_detected=[], gaps_remaining=[],
        data_complete=True, interval="5m",
    )
    market = SimpleNamespace(
        fetch_historical_closed=AsyncMock(return_value=bundle),
        fetch_live_price=AsyncMock(),
        is_quote_stale=lambda quote: False,
    )
    await monitor_conditional_plans(session, market)
    assert watch.status == "ready"
    assert watch.last_reason == "auto_open_disabled"
    market.fetch_live_price.assert_not_called()
    n = await session.scalar(select(func.count()).select_from(SimPosition))
    assert n == 0
