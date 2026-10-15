"""Persist AI WAIT plans and activate them from verified closed ICT candles.

Paper-only, fail-closed on missing data. No extra ChatGPT consumption.
Auto entry uses existing explicit user opt-in and the existing paper order service.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.models import AiConditionalPlan, AiProposedSetup
from app.services.ai_setup_memory import create_setup_from_structured, validate_proposed_setup_levels
from app.services.conditional_ict import REFERENCE_BARS, STEP_MS, watch_closed_bars
from app.services.execution_model import ExecutionModel
from app.services.market_data import MarketDataService
from app.services.paper_auto_trading import maybe_auto_open_paper_position
from app.services.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)
MAX_WATCHING_PER_SYMBOL = 12


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def positive(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _age_hours(structured: dict[str, Any]) -> float:
    proposed = structured.get("proposed_setup") or {}
    if not isinstance(proposed, dict):
        proposed = {}
    hours = positive(proposed.get("expires_hours")) or 24.0
    return max(1.0, min(hours, 48.0))


async def persist_conditional_plan(
    session: AsyncSession, *, run_id: int, symbol: str, structured: dict[str, Any]
) -> dict[str, Any]:
    """WAIT is stored separately: NO_TRADE remains NO_TRADE in the AI audit."""
    meta: dict[str, Any] = {"created": False, "skipped": True}
    decision = str(structured.get("decision") or "NO_TRADE").upper()
    mode = str(structured.get("setup_mode") or "").lower()
    final = str(structured.get("final_ai_decision") or "").upper()
    if decision != "NO_TRADE" or (mode != "conditional" and final != "WAIT"):
        return {**meta, "reason": "not_conditional_wait"}

    direction = str(structured.get("potential_direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return {**meta, "reason": "missing_potential_direction"}
    low = positive(structured.get("conditional_entry_zone_low") or structured.get("entry_zone_low"))
    high = positive(structured.get("conditional_entry_zone_high") or structured.get("entry_zone_high"))
    if low is None or high is None or low >= high:
        return {**meta, "reason": "missing_numeric_entry_zone"}

    entry = positive(structured.get("conditional_entry_price") or structured.get("entry_price"))
    if entry is None:
        entry = (low + high) / 2
    sl = positive(structured.get("conditional_stop_loss") or structured.get("stop_loss"))
    tp = positive(structured.get("conditional_take_profit") or structured.get("take_profit"))
    if sl is None or tp is None or not (low <= entry <= high):
        return {**meta, "reason": "missing_or_outside_plan_levels"}
    levels_ok, err = validate_proposed_setup_levels(direction, entry, sl, tp)
    if not levels_ok:
        return {**meta, "reason": err}

    # Do not store an unlimited number of near-identical WAIT ideas.
    active = (
        await session.execute(
            select(AiConditionalPlan)
            .where(AiConditionalPlan.symbol == symbol, AiConditionalPlan.status.in_(("watching", "ready")))
            .order_by(desc(AiConditionalPlan.id))
            .limit(25)
        )
    ).scalars().all()
    for existing in active:
        if existing.direction != direction:
            continue
        if abs(existing.entry_price - entry) / entry <= 0.0025:
            return {
                "created": False, "skipped": True,
                "reason": "duplicate_active_plan", "plan_id": existing.id,
            }
    if len(active) >= MAX_WATCHING_PER_SYMBOL:
        return {**meta, "reason": "max_active_watch_plans"}

    row = AiConditionalPlan(
        source_run_id=run_id,
        symbol=symbol,
        direction=direction,
        zone_low=low,
        zone_high=high,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=_age_hours(structured)),
        status="watching",
        evidence={
            "ai_confirmation_required": str(structured.get("confirmation_required") or "")[:500],
            "ai_conditional_invalidation": str(structured.get("conditional_invalidation") or "")[:500],
            "entry_poi_type": str(structured.get("entry_poi_type") or "")[:60],
        },
        last_reason="waiting_closed_candle",
    )
    session.add(row)
    await session.flush()
    return {"created": True, "skipped": False, "plan_id": row.id, "status": "watching"}


def _plan_structured(plan: AiConditionalPlan) -> dict[str, Any]:
    ev = dict(plan.evidence or {})
    return {
        "decision": plan.direction,
        "final_ai_decision": plan.direction,
        "setup_mode": "immediate",
        "entry_price": plan.entry_price,
        "entry_zone_low": plan.zone_low,
        "entry_zone_high": plan.zone_high,
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit,
        "liquidity_event_clear": ev.get("sweep_ms") is not None,
        "poi_reaction_clear": ev.get("poi_ms") is not None,
        "structural_confirmation_clear": ev.get("structure_ms") is not None,
        "logical_invalidation_clear": True,
        "watch_provenance": {
            "type": "post_ai_closed_5m_candles",
            "conditional_plan_id": plan.id,
            "source_ai_run_id": plan.source_run_id,
            "evidence": ev,
        },
        "proposed_setup": {
            "new_setup": True,
            "distinct_new_setup": False,
            "thesis_fa": "Activated from verified 5m conditional ICT watch",
            "expires_hours": max(1, min(48, (utc(plan.expires_at) - datetime.now(timezone.utc)).total_seconds() / 3600)),
        },
    }


def _start_ms(plan: AiConditionalPlan) -> int:
    created_ms = int(utc(plan.created_at).timestamp() * 1000)
    return (created_ms // STEP_MS + 1) * STEP_MS


async def monitor_conditional_plans(
    session: AsyncSession, market: MarketDataService | None = None
) -> list[dict[str, Any]]:
    """Run under BotWorker's existing distributed lock. 5m data; cycle every 1–5m."""
    market = market or MarketDataService()
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    runtime = await get_runtime_settings(session)
    rows = (
        await session.execute(
            select(AiConditionalPlan)
            .where(AiConditionalPlan.status.in_(("watching", "ready")))
            .order_by(AiConditionalPlan.id)
            .limit(50)
        )
    ).scalars().all()
    results: list[dict[str, Any]] = []

    for plan in rows:
        if utc(plan.expires_at) <= now:
            plan.status, plan.last_reason = "expired", "plan_expired"
            results.append({"plan_id": plan.id, "status": plan.status, "reason": plan.last_reason})
            continue
        start_ms = _start_ms(plan)
        expected = (plan.last_checked_candle_ms + STEP_MS) if plan.last_checked_candle_ms else start_ms
        from_ms = max(0, expected - REFERENCE_BARS * STEP_MS - STEP_MS)
        try:
            bundle = await market.fetch_historical_closed(plan.symbol, "5m", from_ms, max_bars=700)
        except Exception as exc:
            logger.warning("Conditional watch %s data error: %s", plan.id, type(exc).__name__)
            plan.last_reason = "market_fetch_failed"
            continue
        if not bundle.data_complete or bundle.gaps_remaining:
            plan.last_reason = "market_data_incomplete"
            continue

        expired_ms = int(utc(plan.expires_at).timestamp() * 1000)
        outcome = watch_closed_bars(
            bundle.closed,
            direction=plan.direction,
            zone_low=plan.zone_low,
            zone_high=plan.zone_high,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            start_ms=start_ms,
            cursor_ms=plan.last_checked_candle_ms or 0,
            evidence=plan.evidence,
            now_ms=now_ms,
            expires_ms=expired_ms,
            already_ready=plan.status == "ready",
        )
        plan.evidence = outcome.evidence
        plan.last_checked_candle_ms = outcome.cursor_ms
        plan.last_reason = outcome.reason
        if outcome.state == "invalidated":
            plan.status = "invalidated"
            results.append({"plan_id": plan.id, "status": plan.status, "reason": plan.last_reason})
            continue
        if outcome.reason in ("history_gap", "insufficient_reference_bars"):
            continue
        if outcome.state != "ready":
            continue

        # The SL must invalidate the recorded sweep; these booleans are based
        # on observed candles, NEVER a copied AI self-assessment.
        sweep_extreme = positive(plan.evidence.get("sweep_extreme"))
        if sweep_extreme is None or (
            plan.direction == "LONG" and not plan.stop_loss < sweep_extreme
        ) or (
            plan.direction == "SHORT" and not plan.stop_loss > sweep_extreme
        ):
            plan.status, plan.last_reason = "invalidated", "sl_not_beyond_sweep"
            continue

        if not (plan.zone_low <= plan.entry_price <= plan.zone_high):
            plan.status, plan.last_reason = "invalidated", "entry_outside_zone"
            continue

        rr = ExecutionModel().calc_rr(
            plan.direction, plan.entry_price, plan.stop_loss, plan.take_profit,
            "LIMIT", min_net_rr=max(2.0, runtime.min_net_rr),
        )
        if not rr.acceptable:
            plan.last_reason = "net_rr_below_2_or_runtime_min"
            plan.status = "ready"
            continue

        plan.status = "ready"
        structured = _plan_structured(plan)

        # Persist at most once. Repeated worker cycles do not duplicate setups.
        if not plan.linked_setup_id:
            setup, persistence = await create_setup_from_structured(
                session, run_id=plan.source_run_id, symbol=plan.symbol, structured=structured
            )
            if setup is None:
                plan.last_reason = "setup_" + str(persistence.get("reason") or "not_created")[:75]
                continue
            plan.linked_setup_id = setup.setup_id
        else:
            setup = (
                await session.execute(
                    select(AiProposedSetup).where(AiProposedSetup.setup_id == plan.linked_setup_id)
                )
            ).scalar_one_or_none()
            if setup is None:
                plan.status, plan.last_reason = "invalidated", "linked_setup_missing"
                continue

        if utc(setup.expires_at) <= now or setup.status != "open":
            plan.status, plan.last_reason = "expired", "linked_setup_not_active"
            continue

        if runtime.paper_trading_paused:
            plan.last_reason = "paper_trading_paused"
            continue

        # Limit orders must be on the non-marketable side; no instant/backdated fills.
        try:
            quote = await market.fetch_live_price(plan.symbol)
            if market.is_quote_stale(quote):
                plan.last_reason = "stale_live_price"
                continue
        except Exception:
            plan.last_reason = "live_price_fetch_failed"
            continue
        if (plan.direction == "LONG" and quote.price <= plan.entry_price) or (
            plan.direction == "SHORT" and quote.price >= plan.entry_price
        ):
            plan.last_reason = "entry_already_crossed_wait_next_confirmed_plan"
            continue

        meta = await maybe_auto_open_paper_position(
            session,
            run_id=plan.source_run_id,
            setup=setup,
            validation={"ai_decision": plan.direction, "acceptable_rr": rr.acceptable, "net_rr": rr.net_rr},
            structured=structured,
            symbol=plan.symbol,
            commit=False,
        )
        if meta.get("created"):
            plan.status, plan.last_reason = "activated", "paper_limit_order_created"
        else:
            plan.last_reason = str(meta.get("reason") or "auto_entry_not_created")[:96]
        results.append({"plan_id": plan.id, "status": plan.status, "reason": plan.last_reason})

    await session.flush()
    return results
