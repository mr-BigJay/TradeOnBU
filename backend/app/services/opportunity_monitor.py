from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_effective_settings
from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.market_freshness import is_market_data_fresh
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
    WAIT_LONG,
    WAIT_SHORT,
    ready_state_for_direction,
)
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.domain.time_utils import ensure_utc
from app.models import AiProposedSetup, SimPosition
from app.services.execution_model import ExecutionModel
from app.services.ict_confirmation_engine import all_required_met, process_bar_sequential
from app.services.market_data import MarketDataService
from app.services.opportunity_audit import log_setup_event
from app.services.opportunity_monitor_lock import (
    new_monitor_owner,
    release_opportunity_monitor_lock,
    try_acquire_opportunity_monitor_lock,
)
from app.services.paper_auto_trading import maybe_auto_open_paper_position

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = "5m"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _setup_expired(setup: AiProposedSetup, now: datetime) -> bool:
    exp = ensure_utc(setup.expires_at)
    if exp is None:
        return False
    now_u = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return exp <= now_u


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


def _confirmation_sequence(setup: AiProposedSetup) -> list[str]:
    state = setup.confirmation_state or {}
    seq = state.get("sequence") or state.get("required")
    if isinstance(seq, list) and seq:
        return [str(x) for x in seq]
    plan = setup.conditional_plan or {}
    seq2 = plan.get("confirmation_sequence")
    if isinstance(seq2, list) and seq2:
        return [str(x) for x in seq2]
    return []


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


def _eligible_closed_bars(
    setup: AiProposedSetup,
    closed: list,
    now_ms: int,
) -> list:
    boundary = setup.monitor_from_ms or first_eligible_bar_open_ms(setup.created_at, MONITOR_INTERVAL)
    cursor = setup.monitor_cursor_ms if setup.monitor_cursor_ms else boundary - 1
    step = interval_ms(MONITOR_INTERVAL)
    out = []
    for bar in closed:
        if bar.time_ms < boundary:
            continue
        if bar.time_ms <= cursor:
            continue
        if now_ms < candle_close_ms(bar, MONITOR_INTERVAL):
            continue
        out.append(bar)
    return out


class OpportunityMonitorService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    async def monitor_setups(self, session: AsyncSession, *, symbol: str | None = None) -> dict[str, Any]:
        settings = get_effective_settings()
        symbol = symbol or settings.default_symbol
        now = _utc_now()
        now_ms = int(now.timestamp() * 1000)

        result = await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status == "open")
            .where(AiProposedSetup.monitoring_enabled.is_(True))
            .where(
                AiProposedSetup.lifecycle_state.in_(
                    (WAIT_LONG, WAIT_SHORT, READY_LONG, READY_SHORT, PENDING_ENTRY, POSITION_OPEN)
                )
            )
        )
        setups = list(result.scalars().all())
        summary: dict[str, Any] = {"checked": len(setups), "promoted": 0, "invalidated": 0, "expired": 0, "orders": 0}

        bundle = await self.market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, 120)
        if not bundle.data_complete or not bundle.closed:
            for setup in setups:
                setup.last_rejection_reason = REASON_MISSING_MARKET_DATA
                setup.last_validation_result = {"ok": False, "reason": REASON_MISSING_MARKET_DATA}
            await session.flush()
            return summary

        fresh, age_sec, allowed_age = is_market_data_fresh(
            bundle.closed[-1], MONITOR_INTERVAL, now_ms, settings.market_data_stale_seconds
        )
        if not fresh:
            for setup in setups:
                setup.last_rejection_reason = REASON_STALE_MARKET_DATA
                setup.last_validation_result = {
                    "ok": False,
                    "reason": REASON_STALE_MARKET_DATA,
                    "age_since_close_sec": age_sec,
                    "allowed_age_sec": allowed_age,
                }
            await session.flush()
            return summary

        for setup in setups:
            await _sync_position_lifecycle(session, setup)
            if setup.lifecycle_state in (PENDING_ENTRY, POSITION_OPEN, POSITION_CLOSED):
                continue

            if _setup_expired(setup, now):
                setup.status = "expired"
                setup.lifecycle_state = EXPIRED
                setup.last_rejection_reason = REASON_SETUP_EXPIRED
                await log_setup_event(session, setup_id=setup.setup_id, event_type="expired", reason_code=REASON_SETUP_EXPIRED)
                summary["expired"] += 1
                continue

            sequence = _confirmation_sequence(setup)
            if not sequence or setup.invalidation_level is None:
                setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                setup.last_validation_result = {"ok": False, "reason": "missing_structured_monitor_requirements"}
                continue

            boundary = setup.monitor_from_ms or first_eligible_bar_open_ms(setup.created_at, MONITOR_INTERVAL)
            new_bars = _eligible_closed_bars(setup, bundle.closed, now_ms)
            if not new_bars:
                setup.last_monitor_at = now
                continue

            zone_lo, zone_hi = _zone_bounds(setup)
            conf_state = dict(setup.confirmation_state or {})
            invalidated = False
            inv_reason: str | None = None

            for bar in new_bars:
                # Pre-creation bars supply swing context; confirmations only on bars >= boundary.
                window = [
                    c
                    for c in bundle.closed
                    if c.time_ms <= bar.time_ms and now_ms >= candle_close_ms(c, MONITOR_INTERVAL)
                ][-48:]
                progress = process_bar_sequential(
                    direction=setup.direction,
                    sequence=sequence,
                    confirmation_state=conf_state,
                    bar=bar,
                    window=window,
                    zone_lo=zone_lo,
                    zone_hi=zone_hi,
                    invalidation_level=setup.invalidation_level,
                    structured=setup.structured_snapshot or {},
                    monitor_from_ms=boundary,
                )
                conf_state = {
                    "sequence": sequence,
                    "required": sequence,
                    "completed": progress.completed,
                    "missing": progress.missing,
                    "confirmation_timestamps": progress.confirmation_timestamps,
                }
                if progress.invalidated:
                    invalidated = True
                    inv_reason = progress.invalidation_reason
                    break
                setup.monitor_cursor_ms = bar.time_ms

            setup.confirmation_state = conf_state
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

            from app.services.ict_confirmation_engine import ConfirmationProgress

            progress_check = ConfirmationProgress(
                sequence=sequence,
                completed=conf_state.get("completed") or [],
                missing=conf_state.get("missing") or [],
                confirmation_timestamps=conf_state.get("confirmation_timestamps") or {},
            )

            if setup.lifecycle_state in (WAIT_LONG, WAIT_SHORT):
                if not all_required_met(progress_check):
                    setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_MISSING_CONFIRMATIONS,
                        "missing": progress_check.missing,
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

                claim = await session.execute(
                    update(AiProposedSetup)
                    .where(AiProposedSetup.id == setup.id)
                    .where(AiProposedSetup.lifecycle_state.in_((READY_LONG, READY_SHORT)))
                    .where(AiProposedSetup.setup_id == setup.setup_id)
                    .values(lifecycle_state=PENDING_ENTRY)
                )
                await session.flush()
                if claim.rowcount != 1:
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
                    summary["orders"] += 1
                    await log_setup_event(session, setup_id=setup.setup_id, event_type="order_created", payload=auto)
                else:
                    setup.lifecycle_state = ready_state_for_direction(setup.direction)
                    setup.last_rejection_reason = auto.get("reason")
                    if auto.get("reason") == "wallet_not_initialized":
                        setup.last_validation_result = {"ok": False, "reason": REASON_WALLET_NOT_INITIALIZED}

        await session.flush()
        return summary


async def run_opportunity_monitor_tick(session: AsyncSession) -> dict[str, Any]:
    owner = new_monitor_owner()
    if not await try_acquire_opportunity_monitor_lock(session, owner):
        return {"skipped": True, "reason": "monitor_lock_held"}
    try:
        svc = OpportunityMonitorService()
        return await svc.monitor_setups(session)
    finally:
        await release_opportunity_monitor_lock(session, owner)
