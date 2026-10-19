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
    ACTIVE_MONITOR_STATES,
    EXPIRED,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_CLOSED,
    POSITION_OPEN,
    DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
    ICT_CONFIRMED_LONG,
    ICT_CONFIRMED_SHORT,
    READY_LONG,
    READY_SHORT,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
    REASON_MISSING_CONFIRMATIONS,
    REASON_MISSING_MARKET_DATA,
    REASON_NET_RR_BELOW_MIN,
    REASON_SETUP_EXPIRED,
    REASON_STALE_MARKET_DATA,
    REASON_AUTO_PAPER_DISABLED,
    REASON_STRUCTURAL_INVALIDATION,
    REASON_WALLET_NOT_INITIALIZED,
    WAIT_LONG,
    WAIT_SHORT,
    ict_confirmed_state_for_direction,
    ready_state_for_direction,
    wait_state_for_direction,
)
from app.domain.execution_order_policy import (
    reconcile_pending_ai_limit_order,
    verify_five_m_for_pending_fills,
)
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.domain.time_utils import ensure_utc
from app.models import AiProposedSetup, SimPosition
from app.services.ict_confirmation_engine import (
    _check_invalidation_on_bar,
    process_bar_sequential,
)
from app.services.position.position_service import SimPositionService
from app.services.market_data import MarketDataService
from app.services.monitor_kline_backfill import (
    detect_skipped_closed_bars,
    first_discontinuity_open_ms,
)
from app.services.monitor_kline_fetch import (
    fetch_monitor_5m_bundle,
    setup_history_covers_cursor,
)
from app.services.monitor_kline_backfill import live_monitor_fetch_plan
from app.services.opportunity_audit import log_setup_event
from app.services.opportunity_timeline import append_timeline_event
from app.services.opportunity_monitor_lock import (
    new_monitor_owner,
    refresh_opportunity_monitor_lock,
    release_opportunity_monitor_lock,
    try_acquire_opportunity_monitor_lock,
)
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.services.balanced_confirmation_engine import (
    advance_balanced_on_closed_5m_bar,
    pre_entry_requirements_met,
    required_confirmations_met,
)
from app.services.entry_execution_pipeline import (
    balanced_promotion_ready,
    strict_promotion_ready,
    sync_pipeline_after_monitor,
)
from app.services.entry_execution_settings import get_paper_entry_execution_config
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
    return lc in (
        READY_LONG,
        READY_SHORT,
        ICT_CONFIRMED_LONG,
        ICT_CONFIRMED_SHORT,
        PENDING_ENTRY,
        POSITION_OPEN,
        POSITION_CLOSED,
    )


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


async def _invalidate_setup_structurally(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    bar,
    pos: SimPosition | None,
    summary: dict[str, Any],
    event_type: str = "invalidated",
) -> None:
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
        event_type=event_type,
        reason_code=REASON_STRUCTURAL_INVALIDATION,
        payload={"bar_ms": bar.time_ms, "position_id": pos.public_id if pos else None},
    )
    summary["invalidated"] += 1
    setup.monitor_cursor_ms = bar.time_ms


async def _monitor_ready_structural_invalidation_before_order(
    session: AsyncSession,
    setup: AiProposedSetup,
    bundle,
    now_ms: int,
    now: datetime,
    summary: dict[str, Any],
) -> bool:
    """Apply closed 5m structural invalidations before READY→PENDING order creation."""
    if setup.invalidation_level is None:
        return False
    new_bars = _eligible_closed_bars_since_cursor(setup, bundle.closed, now_ms)
    if not new_bars:
        setup.last_monitor_at = now
        return False
    for bar in new_bars:
        if _check_invalidation_on_bar(
            bar, direction=setup.direction, invalidation_level=setup.invalidation_level
        ):
            await _invalidate_setup_structurally(
                session, setup, bar=bar, pos=None, summary=summary, event_type="ready_invalidated"
            )
            return True
        setup.monitor_cursor_ms = bar.time_ms
    setup.last_monitor_at = now
    return False


async def _monitor_pending_structural_invalidation(
    session: AsyncSession,
    setup: AiProposedSetup,
    bundle,
    now_ms: int,
    now: datetime,
    summary: dict[str, Any],
    *,
    market: MarketDataService,
) -> None:
    if setup.invalidation_level is None:
        return
    pos = await _pending_position_for_setup(session, setup.setup_id)
    new_bars = _eligible_closed_bars_since_cursor(setup, bundle.closed, now_ms)
    settings = get_effective_settings()
    closed_5m = list(bundle.closed) if bundle.closed else []
    order_ms_for_verify = 0
    if pos:
        order_ms_for_verify = pos.monitor_from_ms or 0
        if order_ms_for_verify <= 0 and pos.created_at:
            created = ensure_utc(pos.created_at)
            if created:
                order_ms_for_verify = int(created.timestamp() * 1000)
    five_m_verified, _five_m_reason = verify_five_m_for_pending_fills(
        data_complete=bundle.data_complete,
        closed_5m=closed_5m,
        now_ms=now_ms,
        configured_stale_seconds=settings.market_data_stale_seconds,
        order_ms=order_ms_for_verify,
        monitor_cursor_ms=setup.monitor_cursor_ms,
    )

    if pos:
        order_ms = order_ms_for_verify
        bundle_1m = await market.fetch_kline_bundle(setup.symbol, "1m", 120)
        closed_1m = list(bundle_1m.closed) if bundle_1m.closed else []
        if not five_m_verified:
            notes = dict(setup.objective_notes or {})
            notes["pending_fill_suspended"] = True
            notes["pending_fill_block_reason"] = _five_m_reason or "missing_or_incomplete_5m_data"
            setup.objective_notes = notes
            setup.last_monitor_at = now
            return
        outcome = reconcile_pending_ai_limit_order(
            order_ms=order_ms,
            direction=setup.direction,
            invalidation_level=setup.invalidation_level,
            monitor_cursor_ms=setup.monitor_cursor_ms,
            limit_price=float(pos.limit_entry_price or setup.entry_price or 0),
            closed_1m=closed_1m,
            closed_5m=closed_5m,
            now_ms=now_ms,
            five_m_data_verified=True,
            one_m_data_complete=bundle_1m.data_complete,
            gaps_remaining=list(bundle_1m.gaps_remaining or []),
        )
        if outcome.action == "suspend":
            notes = dict(setup.objective_notes or {})
            notes["pending_fill_suspended"] = True
            notes["pending_fill_block_reason"] = outcome.reason
            setup.objective_notes = notes
            setup.last_monitor_at = now
            return
        if outcome.action == "fill":
            setup.last_monitor_at = now
            return
        if outcome.action == "cancel" and outcome.invalidation_bar_5m is not None:
            await _invalidate_setup_structurally(
                session,
                setup,
                bar=outcome.invalidation_bar_5m,
                pos=pos,
                summary=summary,
                event_type="pending_invalidated",
            )
            return

    if not new_bars:
        setup.last_monitor_at = now
        return
    for bar in new_bars:
        if _check_invalidation_on_bar(
            bar, direction=setup.direction, invalidation_level=setup.invalidation_level
        ):
            await _invalidate_setup_structurally(
                session,
                setup,
                bar=bar,
                pos=pos,
                summary=summary,
                event_type="pending_invalidated",
            )
            return
        setup.monitor_cursor_ms = bar.time_ms
    setup.last_monitor_at = now


def _promotion_confirmations_met(
    *,
    entry_mode: str,
    sequence: list[str],
    conf_state: dict[str, Any],
    structured: dict[str, Any],
) -> bool:
    if entry_mode == ENTRY_MODE_STRICT:
        return strict_promotion_ready(
            sequence=sequence,
            confirmation_state=conf_state,
            structured=structured,
        )
    return balanced_promotion_ready(
        sequence=sequence,
        confirmation_state=conf_state,
        structured=structured,
        allow_pre_entry=True,
    )


async def _attempt_ready_paper_order(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    now: datetime,
    summary: dict[str, Any],
    entry_mode: str = ENTRY_MODE_STRICT,
    balanced_shadow: bool = False,
) -> bool:
    """Create paper order while READY; transition to PENDING_ENTRY only on success (atomic savepoint)."""
    from app.services.conditional_revision import objective_confirmations_satisfied_for_order
    from app.services.paper_auto_trading import _has_position_for_setup

    if _setup_expired(setup, now):
        setup.status = "expired"
        setup.lifecycle_state = EXPIRED
        setup.last_rejection_reason = REASON_SETUP_EXPIRED
        return False

    append_timeline_event(setup, "paper_order_attempt", lifecycle=setup.lifecycle_state, at_ms=int(now.timestamp() * 1000))

    sequence = _confirmation_sequence(setup)
    conf_state = setup.confirmation_state or {}
    structured = setup.structured_snapshot or {}
    allow_pre_entry = (
        entry_mode == ENTRY_MODE_BALANCED
        and pre_entry_requirements_met(
            sequence=sequence,
            confirmation_state=conf_state,
            structured=structured,
        )
        and not required_confirmations_met(
            sequence=sequence,
            confirmation_state=conf_state,
            structured=structured,
        )
    )

    if entry_mode == ENTRY_MODE_BALANCED and balanced_shadow:
        append_timeline_event(
            setup,
            "balanced_shadow_skip_order",
            would_allow_pre_entry=allow_pre_entry,
            at_ms=int(now.timestamp() * 1000),
        )
        setup.last_validation_result = {
            **(setup.last_validation_result or {}),
            "ok": True,
            "shadow_mode": True,
            "would_create_order": True,
            "allow_pre_entry": allow_pre_entry,
        }
        return False

    conf_ok, reject_reason = objective_confirmations_satisfied_for_order(
        setup,
        allow_pre_entry_without_touch=allow_pre_entry,
    )
    if not conf_ok:
        setup.lifecycle_state = wait_state_for_direction(setup.direction)
        setup.last_rejection_reason = reject_reason or REASON_MISSING_CONFIRMATIONS
        setup.last_validation_result = {
            "ok": False,
            "reason": reject_reason or REASON_MISSING_CONFIRMATIONS,
            "missing": (setup.confirmation_state or {}).get("missing") or [],
            "ict_confirmed": False,
        }
        return False

    if await _has_position_for_setup(session, setup.setup_id):
        setup.lifecycle_state = PENDING_ENTRY
        return True

    rr_ok, net_rr = await _net_rr_ok(session, setup)
    if not rr_ok:
        setup.last_rejection_reason = REASON_NET_RR_BELOW_MIN
        setup.last_validation_result = {
            "ok": False,
            "reason": REASON_NET_RR_BELOW_MIN,
            "net_rr": net_rr,
            "ict_confirmed": True,
        }
        return False

    validation = {
        "ai_decision": setup.direction,
        "acceptable_rr": True,
        "objective_ready": True,
        "net_rr": net_rr,
    }
    structured = dict(setup.structured_snapshot or {})
    structured["decision"] = setup.direction

    from app.services.position.settings_service import get_or_create_settings
    from app.services.runtime_settings import get_runtime_settings

    await get_or_create_settings(session)
    await get_runtime_settings(session)
    direction = setup.direction

    try:
        async with session.begin_nested():
            auto = await maybe_auto_open_paper_position(
                session,
                run_id=setup.run_id,
                setup=setup,
                validation=validation,
                structured=structured,
                symbol=setup.symbol,
                commit=False,
                require_ai_flags=False,
                allow_pre_entry_without_touch=allow_pre_entry,
                entry_mode=entry_mode,
            )
            if not auto.get("created"):
                setup.lifecycle_state = ready_state_for_direction(direction)
                reason = auto.get("reason") or "auto_open_skipped"
                if reason in ("auto_open_disabled", "auto_paper_disabled"):
                    setup.last_rejection_reason = REASON_AUTO_PAPER_DISABLED
                setup.last_validation_result = {
                    "ok": True,
                    "ready_for_paper": True,
                    "order_skipped_reason": reason,
                    "net_rr": net_rr,
                }
                return False
            setup.lifecycle_state = PENDING_ENTRY
            setup.last_validation_result = {
                "ok": True,
                "net_rr": net_rr,
                "order_created_at": now.isoformat(),
            }
            append_timeline_event(setup, "paper_order_created", net_rr=net_rr, at_ms=int(now.timestamp() * 1000))
            await log_setup_event(
                session, setup_id=setup.setup_id, event_type="order_created", payload=auto
            )
    except Exception:
        await session.refresh(setup)
        setup.lifecycle_state = ready_state_for_direction(direction)
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
            .where(AiProposedSetup.lifecycle_state.in_(tuple(ACTIVE_MONITOR_STATES)))
        )
        setups = list(result.scalars().all())
        summary: dict[str, Any] = {"checked": len(setups), "promoted": 0, "invalidated": 0, "expired": 0, "orders": 0}

        paper_entry_mode, balanced_shadow = await get_paper_entry_execution_config(session)
        summary["paper_entry_mode"] = paper_entry_mode
        summary["balanced_entry_shadow"] = balanced_shadow

        bundle, fetch_meta = await fetch_monitor_5m_bundle(self.market, symbol, setups, now_ms=now_ms)
        kline_limit = fetch_meta.get("aggregate_limit", 120)
        summary["kline_limit"] = kline_limit
        summary["backfill_meta"] = fetch_meta.get("per_setup") or []
        summary["fetch_meta"] = fetch_meta

        closed_1m: list = []
        if paper_entry_mode == ENTRY_MODE_BALANCED:
            bundle_1m = await self.market.fetch_kline_bundle(symbol, "1m", max(300, kline_limit * 5))
            closed_1m = list(bundle_1m.closed or [])

        if not bundle.closed:
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
                    session, setup, bundle, now_ms, now, summary, market=self.market
                )
                continue
            if setup.lifecycle_state in (POSITION_OPEN, POSITION_CLOSED):
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
            cursor_eff = _monitor_cursor_effective(setup, boundary)
            fetch_plan = live_monitor_fetch_plan(
                setup_id=setup.setup_id,
                boundary_ms=boundary,
                cursor_ms=cursor_eff,
                now_ms=now_ms,
            )
            covers, cover_reason = setup_history_covers_cursor(
                plan=fetch_plan,
                closed_bars=bundle.closed,
                now_ms=now_ms,
            )
            skipped_opens = detect_skipped_closed_bars(
                closed_bars=bundle.closed,
                cursor_ms=cursor_eff,
                boundary_ms=boundary,
                now_ms=now_ms,
                interval=MONITOR_INTERVAL,
            )
            gap_suspend = bool(skipped_opens) or not covers
            if skipped_opens:
                append_timeline_event(
                    setup,
                    "monitor_gap_detected",
                    skipped_bar_opens_ms=skipped_opens[:20],
                    kline_limit=kline_limit,
                )
                await log_setup_event(
                    session,
                    setup_id=setup.setup_id,
                    event_type="monitor_gap_detected",
                    reason_code=REASON_MISSING_MARKET_DATA,
                    payload={"skipped": skipped_opens[:20], "kline_limit": kline_limit},
                )

            new_bars = _eligible_closed_bars(setup, bundle.closed, now_ms)
            if not gap_suspend and new_bars:
                jump = first_discontinuity_open_ms(
                    new_bars=new_bars,
                    cursor_ms=cursor_eff,
                    boundary_ms=boundary,
                    interval=MONITOR_INTERVAL,
                )
                if jump is not None:
                    gap_suspend = True
                    skipped_opens = skipped_opens or [jump]
                    append_timeline_event(
                        setup,
                        "monitor_gap_detected",
                        skipped_bar_opens_ms=[jump],
                        kline_limit=kline_limit,
                        reason="discontinuity_before_new_bars",
                    )
                    await log_setup_event(
                        session,
                        setup_id=setup.setup_id,
                        event_type="monitor_gap_detected",
                        reason_code=REASON_MISSING_MARKET_DATA,
                        payload={"skipped": [jump], "kline_limit": kline_limit},
                    )

            if gap_suspend:
                _record_market_data_health(
                    setup,
                    ok=False,
                    reason=REASON_MISSING_MARKET_DATA,
                    now=now,
                    extra={
                        "skipped": skipped_opens[:20],
                        "cover_reason": cover_reason,
                        "exceeds_ceiling": fetch_plan.exceeds_pagination_ceiling,
                    },
                )
                setup.last_monitor_at = now
                if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                    for bar in new_bars:
                        if _check_invalidation_on_bar(
                            bar,
                            direction=setup.direction,
                            invalidation_level=setup.invalidation_level,
                        ):
                            await _invalidate_setup_structurally(
                                session,
                                setup,
                                bar=bar,
                                pos=None,
                                summary=summary,
                                event_type="ready_invalidated",
                            )
                            break
                    if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                        from app.services.conditional_revision import objective_confirmations_satisfied

                        conf_ok, reject_reason = objective_confirmations_satisfied(setup)
                        if not conf_ok:
                            setup.lifecycle_state = wait_state_for_direction(setup.direction)
                            setup.last_rejection_reason = reject_reason or REASON_MISSING_CONFIRMATIONS
                            setup.last_validation_result = {
                                "ok": False,
                                "reason": reject_reason or REASON_MISSING_CONFIRMATIONS,
                                "missing": (setup.confirmation_state or {}).get("missing") or [],
                                "ict_confirmed": False,
                            }
                    continue
                if setup.lifecycle_state in (PENDING_ENTRY, POSITION_OPEN):
                    await _sync_position_lifecycle(session, setup)
                continue

            if not new_bars:
                setup.last_monitor_at = now
                if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                    if await _monitor_ready_structural_invalidation_before_order(
                        session, setup, bundle, now_ms, now, summary
                    ):
                        continue
                    await _attempt_ready_paper_order(
                        session,
                        setup,
                        now=now,
                        summary=summary,
                        entry_mode=paper_entry_mode,
                        balanced_shadow=balanced_shadow,
                    )
                continue

            zone_lo, zone_hi = _zone_bounds(setup)
            structured = setup.structured_snapshot or {}
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
                if paper_entry_mode == ENTRY_MODE_BALANCED:
                    progress = advance_balanced_on_closed_5m_bar(
                        direction=setup.direction,
                        sequence=sequence,
                        confirmation_state=conf_state,
                        bar_5m=bar,
                        closed_5m=bundle.closed,
                        closed_1m=closed_1m,
                        zone_lo=zone_lo,
                        zone_hi=zone_hi,
                        invalidation_level=setup.invalidation_level,
                        structured=structured,
                        monitor_from_ms=boundary,
                        now_ms=now_ms,
                    )
                else:
                    progress = process_bar_sequential(
                        direction=setup.direction,
                        sequence=sequence,
                        confirmation_state=conf_state,
                        bar=bar,
                        window=window,
                        zone_lo=zone_lo,
                        zone_hi=zone_hi,
                        invalidation_level=setup.invalidation_level,
                        structured=structured,
                        monitor_from_ms=boundary,
                    )
                conf_state = {
                    "sequence": sequence,
                    "required": sequence,
                    "completed": progress.completed,
                    "missing": progress.missing,
                    "confirmation_timestamps": progress.confirmation_timestamps,
                    "confirmation_evidence": progress.confirmation_evidence,
                }
                if progress.invalidated:
                    invalidated = True
                    inv_reason = progress.invalidation_reason
                    break
                setup.monitor_cursor_ms = bar.time_ms

            setup.confirmation_state = conf_state
            setup.last_monitor_at = now
            sync_pipeline_after_monitor(
                setup,
                entry_mode=paper_entry_mode,
                shadow=balanced_shadow,
                sequence=sequence,
                confirmation_state=conf_state,
            )

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
                if not _promotion_confirmations_met(
                    entry_mode=paper_entry_mode,
                    sequence=sequence,
                    conf_state=conf_state,
                    structured=structured,
                ):
                    setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_MISSING_CONFIRMATIONS,
                        "missing": progress_check.missing,
                        "ict_confirmed": False,
                    }
                    continue

                rr_ok, net_rr = await _net_rr_ok(session, setup)
                if not rr_ok:
                    setup.lifecycle_state = ict_confirmed_state_for_direction(setup.direction)
                    setup.last_rejection_reason = REASON_ICT_CONFIRMED_BLOCKED_RR
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_ICT_CONFIRMED_BLOCKED_RR,
                        "display_status": DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
                        "net_rr": net_rr,
                        "ict_confirmed": True,
                        "missing": [],
                    }
                    await log_setup_event(
                        session,
                        setup_id=setup.setup_id,
                        event_type="ict_confirmed_blocked_rr",
                        payload={"net_rr": net_rr},
                    )
                    continue

                setup.lifecycle_state = ready_state_for_direction(setup.direction)
                setup.last_rejection_reason = None
                setup.last_validation_result = {
                    "ok": True,
                    "net_rr": net_rr,
                    "promoted_at": now.isoformat(),
                    "paper_entry_mode": paper_entry_mode,
                }
                append_timeline_event(
                    setup,
                    "promoted_ready",
                    net_rr=net_rr,
                    at_ms=now_ms,
                    paper_entry_mode=paper_entry_mode,
                )
                await log_setup_event(session, setup_id=setup.setup_id, event_type="promoted_ready", payload={"net_rr": net_rr})
                summary["promoted"] += 1

            if setup.lifecycle_state in (ICT_CONFIRMED_LONG, ICT_CONFIRMED_SHORT):
                if not _promotion_confirmations_met(
                    entry_mode=paper_entry_mode,
                    sequence=sequence,
                    conf_state=conf_state,
                    structured=structured,
                ):
                    setup.lifecycle_state = wait_state_for_direction(setup.direction)
                    setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                    continue
                rr_ok, net_rr = await _net_rr_ok(session, setup)
                if rr_ok:
                    setup.lifecycle_state = ready_state_for_direction(setup.direction)
                    setup.last_rejection_reason = None
                    setup.last_validation_result = {"ok": True, "net_rr": net_rr, "promoted_at": now.isoformat()}
                    summary["promoted"] += 1
                else:
                    setup.last_rejection_reason = REASON_ICT_CONFIRMED_BLOCKED_RR
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_ICT_CONFIRMED_BLOCKED_RR,
                        "display_status": DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
                        "net_rr": net_rr,
                        "ict_confirmed": True,
                        "missing": [],
                    }

            if setup.lifecycle_state in (READY_LONG, READY_SHORT):
                if not _promotion_confirmations_met(
                    entry_mode=paper_entry_mode,
                    sequence=sequence,
                    conf_state=conf_state,
                    structured=structured,
                ):
                    setup.lifecycle_state = wait_state_for_direction(setup.direction)
                    setup.last_rejection_reason = REASON_MISSING_CONFIRMATIONS
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_MISSING_CONFIRMATIONS,
                        "missing": progress_check.missing,
                        "ict_confirmed": False,
                    }
                    continue
                from app.services.conditional_revision import objective_confirmations_satisfied_for_order

                allow_pre = (
                    paper_entry_mode == ENTRY_MODE_BALANCED
                    and pre_entry_requirements_met(
                        sequence=sequence,
                        confirmation_state=conf_state,
                        structured=structured,
                    )
                    and not required_confirmations_met(
                        sequence=sequence,
                        confirmation_state=conf_state,
                        structured=structured,
                    )
                )
                conf_ok, reject_reason = objective_confirmations_satisfied_for_order(
                    setup,
                    allow_pre_entry_without_touch=allow_pre,
                )
                if not conf_ok:
                    setup.lifecycle_state = wait_state_for_direction(setup.direction)
                    setup.last_rejection_reason = reject_reason or REASON_MISSING_CONFIRMATIONS
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": reject_reason or REASON_MISSING_CONFIRMATIONS,
                        "missing": progress_check.missing,
                        "ict_confirmed": False,
                    }
                    continue
                rr_ok, net_rr = await _net_rr_ok(session, setup)
                if not rr_ok:
                    setup.lifecycle_state = ict_confirmed_state_for_direction(setup.direction)
                    setup.last_rejection_reason = REASON_ICT_CONFIRMED_BLOCKED_RR
                    setup.last_validation_result = {
                        "ok": False,
                        "reason": REASON_ICT_CONFIRMED_BLOCKED_RR,
                        "display_status": DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
                        "net_rr": net_rr,
                        "ict_confirmed": True,
                        "missing": [],
                    }
                    continue
                if await _monitor_ready_structural_invalidation_before_order(
                    session, setup, bundle, now_ms, now, summary
                ):
                    continue
                await _attempt_ready_paper_order(
                    session,
                    setup,
                    now=now,
                    summary=summary,
                    entry_mode=paper_entry_mode,
                    balanced_shadow=balanced_shadow,
                )

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
