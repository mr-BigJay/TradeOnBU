from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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
from app.services.ict_confirmation_engine import (
    _check_invalidation_on_bar,
    all_required_met,
    process_bar_sequential,
)
from app.services.position.position_service import SimPositionService
from app.services.market_data import MarketDataService
from app.services.opportunity_audit import log_setup_event
from app.services.opportunity_monitor_lock import (
    new_monitor_owner,
    refresh_opportunity_monitor_lock,
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


def _lifecycle_preserves_objective_confirmation(lifecycle: str | None) -> bool:
    lc = (lifecycle or "").upper()
    return lc in (READY_LONG, READY_SHORT, PENDING_ENTRY, POSITION_OPEN, POSITION_CLOSED)


def _record_market_data_health(
    setup: AiProposedSetup,
    *,
    ok: bool,
    reason: str,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> None:
    notes = dict(setup.objective_notes or {})
    payload = {"ok": ok, "reason": reason, "at": now.isoformat(), **(extra or {})}
    notes["market_data_health"] = payload
    if setup.lifecycle_state == PENDING_ENTRY:
        notes["pending_fill_suspended"] = not ok
    setup.objective_notes = notes
    if _lifecycle_preserves_objective_confirmation(setup.lifecycle_state):
        return
    if not ok:
        setup.last_rejection_reason = reason
        setup.last_validation_result = {"ok": False, "reason": reason, **(extra or {})}
        return
    if (setup.last_validation_result or {}).get("ok"):
        setup.last_rejection_reason = None


async def _pending_position_for_setup(session: AsyncSession, setup_id: str) -> SimPosition | None:
    result = await session.execute(
        select(SimPosition)
        .where(SimPosition.ai_setup_id == setup_id)
        .where(SimPosition.status == "pending_entry")
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _monitor_pending_structural_invalidation(
    session: AsyncSession,
    setup: AiProposedSetup,
    bundle,
    now_ms: int,
    now: datetime,
    summary: dict[str, Any],
) -> None:
    if setup.invalidation_level is None:
        return
    new_bars = _eligible_closed_bars_since_cursor(setup, bundle.closed, now_ms)
    if not new_bars:
        setup.last_monitor_at = now
        return
    for bar in new_bars:
        if _check_invalidation_on_bar(
            bar, direction=setup.direction, invalidation_level=setup.invalidation_level
        ):
            pos = await _pending_position_for_setup(session, setup.setup_id)
            svc = SimPositionService()
            if pos:
                await svc.cancel_pending(session, pos.public_id, commit=False)
            setup.status = "invalidated"
            setup.lifecycle_state = INVALIDATED
            setup.last_rejection_reason = REASON_STRUCTURAL_INVALIDATION
            setup.last_validation_result = {
                "ok": False,
                "reason": REASON_STRUCTURAL_INVALIDATION,
                "bar_ms": bar.time_ms,
            }
            await log_setup_event(
                session,
                setup_id=setup.setup_id,
                event_type="pending_invalidated",
                reason_code=REASON_STRUCTURAL_INVALIDATION,
                payload={"bar_ms": bar.time_ms, "position_id": pos.public_id if pos else None},
            )
            summary["invalidated"] += 1
            setup.monitor_cursor_ms = bar.time_ms
            return
        setup.monitor_cursor_ms = bar.time_ms
    setup.last_monitor_at = now


async def _attempt_ready_paper_order(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    now: datetime,
    summary: dict[str, Any],
) -> bool:
    """Create paper order while READY; transition to PENDING_ENTRY only on success (atomic savepoint)."""
    from app.services.paper_auto_trading import _has_position_for_setup

    if await _has_position_for_setup(session, setup.setup_id):
        setup.lifecycle_state = PENDING_ENTRY
        return True

    rr_ok, net_rr = await _net_rr_ok(session, setup)
    if not rr_ok:
        setup.last_rejection_reason = REASON_NET_RR_BELOW_MIN
        return False

    validation = {
        "ai_decision": setup.direction,
        "acceptable_rr": True,
        "objective_ready": True,
        "net_rr": net_rr,
    }
    structured = dict(setup.structured_snapshot or {})
    structured["decision"] = setup.direction

    try:
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
        if not auto.get("created"):
            setup.lifecycle_state = ready_state_for_direction(setup.direction)
            return False
        setup.lifecycle_state = PENDING_ENTRY
        setup.last_validation_result = {
            "ok": True,
            "net_rr": net_rr,
            "order_created_at": now.isoformat(),
        }
        await log_setup_event(
            session, setup_id=setup.setup_id, event_type="order_created", payload=auto
        )
    except Exception:
        setup.lifecycle_state = ready_state_for_direction(setup.direction)
        return False

    summary["orders"] += 1
    return True


async def _net_rr_ok(session: AsyncSession, setup: AiProposedSetup) -> tuple[bool, float | None]:
    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    from app.services.execution_model_sim import net_rr_acceptable_for_auto_paper

    entry = setup.entry_price
    sl = setup.stop_loss
    tp = setup.take_profit
    if entry is None or sl is None or tp is None:
        return False, None
    ok, net_rr = await net_rr_acceptable_for_auto_paper(
        session,
        direction=setup.direction,
        entry=float(entry),
        sl=float(sl),
        tp=float(tp),
        min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
    )
    return ok, net_rr


def _monitor_boundary_ms(setup: AiProposedSetup) -> int:
    if setup.monitor_from_ms is not None and setup.monitor_from_ms > 0:
        return setup.monitor_from_ms
    return first_eligible_bar_open_ms(setup.created_at, MONITOR_INTERVAL)


def _monitor_cursor_effective(setup: AiProposedSetup, boundary: int) -> int:
    if setup.monitor_cursor_ms is not None and setup.monitor_cursor_ms >= 0:
        return setup.monitor_cursor_ms
    return boundary - 1


def _eligible_closed_bars(
    setup: AiProposedSetup,
    closed: list,
    now_ms: int,
) -> list:
    boundary = _monitor_boundary_ms(setup)
    cursor = _monitor_cursor_effective(setup, boundary)
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


def _eligible_closed_bars_since_cursor(
    setup: AiProposedSetup,
    closed: list,
    now_ms: int,
) -> list:
    """New fully closed 5m bars after monitor cursor (pending invalidation / fill checks)."""
    cursor = setup.monitor_cursor_ms if setup.monitor_cursor_ms is not None else -1
    if cursor < 0:
        cursor = -1
    out = []
    for bar in closed:
        if bar.time_ms <= cursor:
            continue
        if now_ms < candle_close_ms(bar, MONITOR_INTERVAL):
            continue
        out.append(bar)
    return out


class OpportunityMonitorService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    async def monitor_setups(
        self, session: AsyncSession, *, symbol: str | None = None, lock_owner: str | None = None
    ) -> dict[str, Any]:
        settings = get_effective_settings()
        symbol = symbol or settings.default_symbol
        now = _utc_now()
        now_ms = int(now.timestamp() * 1000)
        if lock_owner:
            await refresh_opportunity_monitor_lock(lock_owner, session)

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
                _record_market_data_health(
                    setup, ok=False, reason=REASON_MISSING_MARKET_DATA, now=now
                )
            await session.flush()
            for setup in setups:
                if setup.lifecycle_state in (PENDING_ENTRY, POSITION_OPEN):
                    await _sync_position_lifecycle(session, setup)
            return summary

        fresh, age_sec, allowed_age = is_market_data_fresh(
            bundle.closed[-1], MONITOR_INTERVAL, now_ms, settings.market_data_stale_seconds
        )
        if not fresh:
            for setup in setups:
                _record_market_data_health(
                    setup,
                    ok=False,
                    reason=REASON_STALE_MARKET_DATA,
                    now=now,
                    extra={"age_since_close_sec": age_sec, "allowed_age_sec": allowed_age},
                )
            await session.flush()
            for setup in setups:
                if setup.lifecycle_state in (PENDING_ENTRY, POSITION_OPEN):
                    await _sync_position_lifecycle(session, setup)
            return summary

        for setup in setups:
            if lock_owner:
                await refresh_opportunity_monitor_lock(lock_owner, session)
            _record_market_data_health(setup, ok=True, reason="market_data_fresh", now=now)
            await _sync_position_lifecycle(session, setup)

            if setup.lifecycle_state == PENDING_ENTRY:
                await _monitor_pending_structural_invalidation(
                    session, setup, bundle, now_ms, now, summary
                )
                continue
            if setup.lifecycle_state in (POSITION_OPEN, POSITION_CLOSED):
                continue

            if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                await _attempt_ready_paper_order(session, setup, now=now, summary=summary)
                if setup.lifecycle_state == PENDING_ENTRY:
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

            boundary = _monitor_boundary_ms(setup)
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

                rr_ok, net_rr = await _net_rr_ok(session, setup)
                if not rr_ok:
                    setup.last_rejection_reason = REASON_NET_RR_BELOW_MIN
                    setup.last_validation_result = {"ok": False, "reason": REASON_NET_RR_BELOW_MIN, "net_rr": net_rr}
                    continue

                setup.lifecycle_state = ready_state_for_direction(setup.direction)
                setup.last_validation_result = {"ok": True, "net_rr": net_rr, "promoted_at": now.isoformat()}
                await log_setup_event(session, setup_id=setup.setup_id, event_type="promoted_ready", payload={"net_rr": net_rr})
                summary["promoted"] += 1

            if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                await _attempt_ready_paper_order(session, setup, now=now, summary=summary)

        await session.flush()
        return summary


async def run_opportunity_monitor_tick(session: AsyncSession) -> dict[str, Any]:
    owner = new_monitor_owner()
    if not await try_acquire_opportunity_monitor_lock(owner):
        return {"skipped": True, "reason": "monitor_lock_held"}
    summary: dict[str, Any] | None = None
    try:
        svc = OpportunityMonitorService()
        summary = await svc.monitor_setups(session, lock_owner=owner)
        await session.commit()
        return summary
    except Exception:
        await session.rollback()
        raise
    finally:
        await release_opportunity_monitor_lock(owner)
