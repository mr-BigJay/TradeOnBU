from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_effective_settings
from app.domain.setup_lifecycle import (
    EXPIRED,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_CLOSED,
    POSITION_OPEN,
    READY_LONG,
    READY_SHORT,
    REASON_MISSING_CONFIRMATIONS,
    REASON_MISSING_MARKET_DATA,
    REASON_NET_RR_BELOW_MIN,
    REASON_SETUP_EXPIRED,
    REASON_STALE_MARKET_DATA,
    REASON_STRUCTURAL_INVALIDATION,
    REASON_WALLET_NOT_INITIALIZED,
    SETUP_MODE_IMMEDIATE,
    WAIT_LONG,
    WAIT_SHORT,
    ready_state_for_direction,
)
from app.models import AiProposedSetup, SimPosition
from app.services.execution_model import ExecutionModel
from app.services.ict_confirmation_engine import ConfirmationProgress, all_required_met, evaluate_confirmations
from app.services.market_data import MarketDataService
from app.services.opportunity_audit import log_setup_event
from app.services.paper_auto_trading import maybe_auto_open_paper_position

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _sync_position_lifecycle(session: AsyncSession, setup: AiProposedSetup) -> None:
    if not setup.setup_id:
        return
    result = await session.execute(
        select(SimPosition)
        .where(SimPosition.ai_setup_id == setup.setup_id)
        .order_by(SimPosition.created_at.desc())
        .limit(1)
    )
    pos = result.scalar_one_or_none()
    if not pos:
        return
    if pos.status == "pending_entry" and setup.lifecycle_state not in (PENDING_ENTRY, POSITION_OPEN):
        setup.lifecycle_state = PENDING_ENTRY
    elif pos.status == "open":
        setup.lifecycle_state = POSITION_OPEN
    elif pos.status in ("closed", "tp_hit", "sl_hit", "cancelled"):
        if setup.lifecycle_state not in (POSITION_CLOSED, EXPIRED, INVALIDATED):
            setup.lifecycle_state = POSITION_CLOSED


def _required_tokens(setup: AiProposedSetup) -> list[str]:
    state = setup.confirmation_state or {}
    req = state.get("required")
    if isinstance(req, list) and req:
        return [str(x) for x in req]
    plan = setup.conditional_plan or {}
    req2 = plan.get("required_confirmations")
    if isinstance(req2, list) and req2:
        return [str(x) for x in req2]
    return ["entry_zone_touch", "liquidity_sweep", "mss"]


def _zone_bounds(setup: AiProposedSetup) -> tuple[float, float]:
    lo = setup.entry_zone_low
    hi = setup.entry_zone_high
    if lo is not None and hi is not None and lo < hi:
        return float(lo), float(hi)
    entry = setup.entry_price or 0.0
    return entry * 0.999, entry * 1.001


def _net_rr_ok(setup: AiProposedSetup) -> tuple[bool, float | None]:
    settings = get_effective_settings()
    entry = setup.entry_price
    sl = setup.stop_loss
    tp = setup.take_profit
    if entry is None or sl is None or tp is None:
        return False, None
    model = ExecutionModel()
    rr = model.calc_rr(setup.direction, float(entry), float(sl), float(tp), "LIMIT", min_net_rr=settings.min_net_rr)
    return bool(rr.acceptable), rr.net_rr


class OpportunityMonitorService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    async def monitor_setups(self, session: AsyncSession, *, symbol: str | None = None) -> dict[str, Any]:
        settings = get_effective_settings()
        symbol = symbol or settings.default_symbol
        now = _utc_now()

        result = await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status == "open")
            .where(
                AiProposedSetup.lifecycle_state.in_(
                    (WAIT_LONG, WAIT_SHORT, READY_LONG, READY_SHORT, PENDING_ENTRY, POSITION_OPEN)
                )
            )
        )
        setups = list(result.scalars().all())
        summary: dict[str, Any] = {"checked": len(setups), "promoted": 0, "invalidated": 0, "expired": 0, "orders": 0}

        bundle = await self.market.fetch_kline_bundle(symbol, "5m", 120)
        if not bundle.data_complete or not bundle.closed:
            for setup in setups:
                setup.last_rejection_reason = REASON_MISSING_MARKET_DATA
                setup.last_validation_result = {"ok": False, "reason": REASON_MISSING_MARKET_DATA}
            await session.flush()
            return summary

        stale_limit = settings.market_data_stale_seconds
        last_close_ms = bundle.closed[-1].time_ms
        age_sec = max(0, int(now.timestamp() * 1000) - last_close_ms) // 1000
        if age_sec > stale_limit:
            for setup in setups:
                setup.last_rejection_reason = REASON_STALE_MARKET_DATA
                setup.last_validation_result = {"ok": False, "reason": REASON_STALE_MARKET_DATA, "age_sec": age_sec}
            await session.flush()
            return summary

        for setup in setups:
            await _sync_position_lifecycle(session, setup)
            if setup.lifecycle_state in (PENDING_ENTRY, POSITION_OPEN, POSITION_CLOSED):
                continue

            if setup.expires_at and setup.expires_at <= now:
                setup.status = "expired"
                setup.lifecycle_state = EXPIRED
                setup.last_rejection_reason = REASON_SETUP_EXPIRED
                await log_setup_event(session, setup_id=setup.setup_id, event_type="expired", reason_code=REASON_SETUP_EXPIRED)
                summary["expired"] += 1
                continue

            cursor = setup.monitor_cursor_ms or 0
            closed = [c for c in bundle.closed if c.time_ms > cursor]
            if not closed:
                setup.last_monitor_at = now
                continue

            required = _required_tokens(setup)
            completed_prior = list((setup.confirmation_state or {}).get("completed") or [])
            zone_lo, zone_hi = _zone_bounds(setup)

            # Incremental evaluation — only bars after cursor (closed candles).
            progress_completed: list[str] = list(completed_prior)
            invalidated = False
            inv_reason: str | None = None
            for bar in closed:
                window = [c for c in bundle.closed if c.time_ms <= bar.time_ms][-40:]
                progress = evaluate_confirmations(
                    direction=setup.direction,
                    required=required,
                    completed_prior=progress_completed,
                    candles=window,
                    zone_lo=zone_lo,
                    zone_hi=zone_hi,
                    invalidation_level=setup.invalidation_level,
                    structured=setup.structured_snapshot or {},
                )
                progress_completed = progress.completed
                if progress.invalidated:
                    invalidated = True
                    inv_reason = progress.invalidation_reason
                    break
                setup.monitor_cursor_ms = bar.time_ms

            setup.confirmation_state = {
                "required": required,
                "completed": progress_completed,
                "missing": [t for t in required if t not in progress_completed],
            }
            setup.last_monitor_at = now

            if invalidated:
                setup.status = "invalidated"
                setup.lifecycle_state = INVALIDATED
                setup.last_rejection_reason = REASON_STRUCTURAL_INVALIDATION
                await log_setup_event(
                    session,
                    setup_id=setup.setup_id,
                    event_type="invalidated",
                    reason_code=REASON_STRUCTURAL_INVALIDATION,
                    payload={"detail": inv_reason},
                )
                summary["invalidated"] += 1
                continue

            if setup.lifecycle_state in (WAIT_LONG, WAIT_SHORT):
                progress_check = ConfirmationProgress(
                    completed=progress_completed,
                    missing=setup.confirmation_state["missing"],
                )
                if not all_required_met(progress_check):
                    setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_MISSING_CONFIRMATIONS,
                        "missing": setup.confirmation_state["missing"],
                    }
                    continue

                rr_ok, net_rr = _net_rr_ok(setup)
                if not rr_ok:
                    setup.last_rejection_reason = REASON_NET_RR_BELOW_MIN
                    setup.last_validation_result = {"ok": False, "reason": REASON_NET_RR_BELOW_MIN, "net_rr": net_rr}
                    continue

                setup.lifecycle_state = ready_state_for_direction(setup.direction)
                setup.last_validation_result = {"ok": True, "net_rr": net_rr, "promoted_at": now.isoformat()}
                await log_setup_event(session, setup_id=setup.setup_id, event_type="promoted_ready", payload={"net_rr": net_rr})
                summary["promoted"] += 1

            if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                rr_ok, net_rr = _net_rr_ok(setup)
                if not rr_ok:
                    setup.last_rejection_reason = REASON_NET_RR_BELOW_MIN
                    continue
                validation = {
                    "ai_decision": setup.direction,
                    "acceptable_rr": True,
                    "objective_ready": True,
                    "net_rr": net_rr,
                }
                structured = dict(setup.structured_snapshot or {})
                structured["decision"] = setup.direction
                auto = await maybe_auto_open_paper_position(
                    session,
                    run_id=setup.run_id,
                    setup=setup,
                    validation=validation,
                    structured=structured,
                    symbol=setup.symbol,
                    commit=False,
                    require_ai_flags=False,
                )
                if auto.get("created"):
                    setup.lifecycle_state = PENDING_ENTRY
                    summary["orders"] += 1
                    await log_setup_event(session, setup_id=setup.setup_id, event_type="order_created", payload=auto)
                elif auto.get("skipped"):
                    setup.last_rejection_reason = auto.get("reason")
                    if auto.get("reason") == "wallet_not_initialized":
                        setup.last_validation_result = {"ok": False, "reason": REASON_WALLET_NOT_INITIALIZED}

        await session.flush()
        return summary


async def run_opportunity_monitor_tick(session: AsyncSession) -> dict[str, Any]:
    svc = OpportunityMonitorService()
    return await svc.monitor_setups(session)
