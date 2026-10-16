from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN, READY_LONG, READY_SHORT
from app.domain.time_utils import ensure_utc
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.ai_setup_memory import validate_proposed_setup_levels
from app.services.market_data import MarketDataService
from app.services.position.execution_adapters import OrderRequest
from app.services.position.position_service import PositionServiceError, SimPositionService
from app.services.position.settings_service import get_or_create_settings
from app.services.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)

_ICT_AUTO_ENTRY_FLAGS: tuple[str, ...] = (
    "liquidity_event_clear",
    "poi_reaction_clear",
    "structural_confirmation_clear",
    "logical_invalidation_clear",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def setup_eligible_for_auto_entry(
    setup: AiProposedSetup,
    *,
    now: datetime | None = None,
    require_ai_flags: bool = True,
) -> tuple[bool, str | None]:
    now = now or _utc_now()
    if setup.status != "open":
        return False, f"setup_status_{setup.status}"
    exp = ensure_utc(setup.expires_at)
    if exp is not None and exp <= (now if now.tzinfo else now.replace(tzinfo=timezone.utc)):
        return False, "setup_expired"
    entry = setup.entry_price
    sl = setup.stop_loss
    tp = setup.take_profit
    if entry is None or sl is None or tp is None:
        return False, "missing_levels"
    ok, err = validate_proposed_setup_levels(setup.direction, entry, sl, tp)
    if not ok:
        return False, err
    if setup.paper_fill_recorded:
        return False, "paper_fill_already_recorded"
    lifecycle = (setup.lifecycle_state or "").upper()
    if lifecycle in (PENDING_ENTRY, POSITION_OPEN):
        return True, None
    objective_ready = lifecycle in (READY_LONG, READY_SHORT) or bool(
        (setup.last_validation_result or {}).get("ok")
    )
    if require_ai_flags and not objective_ready:
        ok_flags, flag_reason = ict_auto_entry_confirmations_clear(setup.structured_snapshot or {})
        if not ok_flags:
            return False, flag_reason
    return True, None


def ict_auto_entry_confirmations_clear(structured: dict[str, Any]) -> tuple[bool, str | None]:
    """All four ICT readiness booleans must be explicitly true for auto paper entry."""
    if not structured:
        return False, "missing_structured_snapshot"
    for key in _ICT_AUTO_ENTRY_FLAGS:
        if structured.get(key) is not True:
            return False, f"{key}_not_clear"
    return True, None


def build_ict_entry_audit_snapshot(
    *,
    structured: dict[str, Any],
    setup: AiProposedSetup,
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Persisted on auto paper positions for post-hoc ICT condition review."""
    decision = str(structured.get("decision") or validation.get("ai_decision") or setup.direction or "").upper()
    return {
        "decision": decision,
        "entry": float(setup.entry_price),  # type: ignore[arg-type]
        "stop_loss": float(setup.stop_loss),  # type: ignore[arg-type]
        "take_profit": float(setup.take_profit),  # type: ignore[arg-type]
        "liquidity_event_clear": structured.get("liquidity_event_clear"),
        "poi_reaction_clear": structured.get("poi_reaction_clear"),
        "structural_confirmation_clear": structured.get("structural_confirmation_clear"),
        "logical_invalidation_clear": structured.get("logical_invalidation_clear"),
    }


def trade_decision_allows_auto_paper(
    structured: dict[str, Any],
    validation: dict[str, Any],
    *,
    require_ai_flags: bool = True,
) -> tuple[bool, str | None]:
    decision = str(structured.get("decision") or validation.get("ai_decision") or "").upper()
    if decision not in ("LONG", "SHORT"):
        return False, "not_trade_decision"
    if validation.get("acceptable_rr") is not True:
        return False, "net_rr_not_acceptable"
    if require_ai_flags and not validation.get("objective_ready"):
        ok_flags, flag_reason = ict_auto_entry_confirmations_clear(structured)
        if not ok_flags:
            return False, flag_reason
    return True, None


async def _active_position_count(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(SimPosition)
        .where(SimPosition.status.in_(("pending_entry", "open")))
    )
    return int(result.scalar_one())


async def _has_position_for_setup(session: AsyncSession, setup_id: str) -> bool:
    result = await session.execute(
        select(SimPosition.id)
        .where(SimPosition.ai_setup_id == setup_id)
        .where(SimPosition.status.in_(("pending_entry", "open")))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _verify_fill_net_rr(
    session: AsyncSession,
    *,
    direction: str,
    fill_price: float,
    sl: float,
    tp: float,
    order_type: str,
) -> tuple[bool, float]:
    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    from app.services.execution_model_sim import net_rr_acceptable_for_auto_paper

    return await net_rr_acceptable_for_auto_paper(
        session,
        direction=direction,
        entry=fill_price,
        sl=sl,
        tp=tp,
        min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
        order_type=order_type,
    )


async def sync_setup_lifecycle_after_auto_order(
    session: AsyncSession,
    setup: AiProposedSetup,
    pos: SimPosition,
) -> str:
    """Return lifecycle state applied to setup."""
    from app.domain.setup_lifecycle import PENDING_ENTRY, POSITION_OPEN

    if pos.status == "open":
        await mark_setup_paper_fill(session, pos)
        setup.lifecycle_state = POSITION_OPEN
        setup.paper_fill_recorded = True
        return POSITION_OPEN
    setup.lifecycle_state = PENDING_ENTRY
    return PENDING_ENTRY


async def preview_auto_paper_entry(
    session: AsyncSession,
    *,
    setup: AiProposedSetup,
    validation: dict[str, Any],
    structured: dict[str, Any],
    symbol: str,
    require_ai_flags: bool = True,
    live_price: float | None = None,
    ticker_time_ms: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dry-run through live price, entry mode, R:R, and dual filters (no budget/order)."""
    meta: dict[str, Any] = {"eligible": False, "skipped": False, "symbol": symbol}

    ok_val, val_reason = trade_decision_allows_auto_paper(
        structured, validation, require_ai_flags=require_ai_flags
    )
    if not ok_val:
        meta["skipped"] = True
        meta["reason"] = val_reason
        return meta

    ok_setup, setup_reason = setup_eligible_for_auto_entry(setup, require_ai_flags=require_ai_flags)
    if not ok_setup:
        meta["skipped"] = True
        meta["reason"] = setup_reason
        return meta

    from app.services.paper_live_price import validate_live_price_for_auto_paper

    clock = now or _utc_now()
    live_ctx = validate_live_price_for_auto_paper(
        live_price, ticker_time_ms=ticker_time_ms, now=clock
    )
    meta["live_price_assessment"] = {
        "ok": live_ctx.ok,
        "reason": live_ctx.reason,
        "ticker_age_seconds": live_ctx.ticker_age_seconds,
    }
    if not live_ctx.ok:
        meta["skipped"] = True
        meta["reason"] = live_ctx.reason
        return meta

    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    from app.services.execution_model_sim import net_rr_acceptable_for_auto_paper
    from app.services.paper_entry_mode import choose_paper_entry_mode
    from app.services.paper_opportunity_filters import evaluate_dual_trade_filters
    from app.services.paper_opportunity_ranking import rank_ready_candidate

    entry = float(setup.entry_price)  # type: ignore[arg-type]
    sl = float(setup.stop_loss)  # type: ignore[arg-type]
    tp = float(setup.take_profit)  # type: ignore[arg-type]
    direction = (setup.direction or "").upper()
    lifecycle_upper = (setup.lifecycle_state or "").upper()

    order_type, skip_entry, mode_diag = choose_paper_entry_mode(
        direction=direction,
        entry=entry,
        live_price=live_price,
        setup_mode=setup.setup_mode,
    )
    meta["entry_mode"] = mode_diag
    meta["order_type"] = order_type
    if skip_entry:
        meta["skipped"] = True
        meta["reason"] = skip_entry
        return meta

    rr_entry = float(live_price) if order_type == "market" and live_price is not None else entry
    rr_ok_mode, net_rr_mode = await net_rr_acceptable_for_auto_paper(
        session,
        direction=direction,
        entry=rr_entry,
        sl=sl,
        tp=tp,
        min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
        order_type=order_type,
    )
    meta["net_rr"] = net_rr_mode
    if not rr_ok_mode:
        meta["skipped"] = True
        meta["reason"] = "net_rr_below_min_at_execution_price"
        return meta

    conf_complete = lifecycle_upper in (READY_LONG, READY_SHORT, PENDING_ENTRY)
    filters_ok, filter_reason, selection_audit = await evaluate_dual_trade_filters(
        session,
        setup,
        structured=structured,
        order_type=order_type,
        mode_diag=mode_diag,
        net_rr=float(net_rr_mode),
        live_price=live_price,
        confirmations_complete=conf_complete,
        now=clock,
    )
    meta["selection_audit"] = selection_audit
    if not filters_ok:
        meta["skipped"] = True
        meta["reason"] = filter_reason
        return meta

    from app.services.paper_opportunity_calibration import fill_calibration_for_bucket, win_calibration_for_bucket
    from app.services.paper_opportunity_filters import hours_to_expiry

    fill_cal = await fill_calibration_for_bucket(
        session,
        direction=direction,
        order_type=order_type,
        distance_bps=float(mode_diag["distance_bps"]) if mode_diag.get("distance_bps") is not None else None,
    )
    win_cal = await win_calibration_for_bucket(
        session,
        direction=direction,
        order_type=order_type,
        distance_bps=float(mode_diag["distance_bps"]) if mode_diag.get("distance_bps") is not None else None,
    )
    rank = rank_ready_candidate(
        order_type=order_type,
        mode_diag=mode_diag,
        net_rr=float(net_rr_mode),
        structured=structured,
        confirmations_complete=conf_complete,
        fill_calibrated_rate=fill_cal.fill_rate,
        win_calibrated_rate=win_cal.win_rate,
        hours_left=hours_to_expiry(setup, clock),
    )
    meta["rank"] = {
        "total": rank.total,
        "fill_feasibility": rank.fill_feasibility,
        "ict_quality": rank.ict_quality,
        "net_rr": rank.net_rr,
        "historical_edge": rank.historical_edge,
        "components": rank.components,
    }
    meta["eligible"] = True
    return meta


async def maybe_auto_open_paper_position(
    session: AsyncSession,
    *,
    run_id: int,
    setup: AiProposedSetup,
    validation: dict[str, Any],
    structured: dict[str, Any],
    symbol: str,
    commit: bool = True,
    require_ai_flags: bool = True,
    live_price: float | None = None,
    ticker_time_ms: int | None = None,
    now: datetime | None = None,
    market: MarketDataService | None = None,
) -> dict[str, Any]:
    """
    Create a paper entry (limit or market) from a validated AI setup when auto mode is enabled.
    Live trading is never used.
    """
    meta: dict[str, Any] = {"attempted": True, "created": False, "skipped": False}

    settings: SimPositionSettings = await get_or_create_settings(session)
    if not settings.auto_open_paper_positions:
        meta["skipped"] = True
        meta["reason"] = "auto_open_disabled"
        return meta

    runtime = await get_runtime_settings(session)
    if runtime.paper_trading_paused:
        meta["skipped"] = True
        meta["reason"] = "paper_trading_paused"
        return meta

    if not settings.initial_balance_set:
        meta["skipped"] = True
        meta["reason"] = "wallet_not_initialized"
        return meta

    from app.services.conditional_revision import objective_confirmations_satisfied

    clock = now or _utc_now()
    lifecycle_upper = (setup.lifecycle_state or "").upper()
    if lifecycle_upper in (READY_LONG, READY_SHORT):
        ok_conf, conf_reason = objective_confirmations_satisfied(setup)
        if not ok_conf:
            meta["skipped"] = True
            meta["reason"] = conf_reason or "missing_confirmations"
            return meta

    ok_val, val_reason = trade_decision_allows_auto_paper(
        structured, validation, require_ai_flags=require_ai_flags
    )
    if not ok_val:
        meta["skipped"] = True
        meta["reason"] = val_reason
        return meta

    lifecycle = (setup.lifecycle_state or "").upper()
    if validation.get("objective_ready") and lifecycle not in (READY_LONG, READY_SHORT, PENDING_ENTRY):
        meta["skipped"] = True
        meta["reason"] = "lifecycle_not_ready"
        return meta

    ok_setup, setup_reason = setup_eligible_for_auto_entry(setup, require_ai_flags=require_ai_flags, now=clock)
    if not ok_setup:
        meta["skipped"] = True
        meta["reason"] = setup_reason
        return meta

    from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
    from app.services.execution_model_sim import net_rr_acceptable_for_auto_paper

    entry_f = float(setup.entry_price)  # type: ignore[arg-type]
    sl_f = float(setup.stop_loss)  # type: ignore[arg-type]
    tp_f = float(setup.take_profit)  # type: ignore[arg-type]
    rr_ok, net_rr = await net_rr_acceptable_for_auto_paper(
        session,
        direction=setup.direction,
        entry=entry_f,
        sl=sl_f,
        tp=tp_f,
        min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
    )
    if not rr_ok:
        meta["skipped"] = True
        meta["reason"] = "net_rr_below_min"
        meta["net_rr"] = net_rr
        meta["required_min_net_rr"] = HARD_MIN_AUTO_PAPER_NET_RR
        return meta

    if await _has_position_for_setup(session, setup.setup_id):
        meta["skipped"] = True
        meta["reason"] = "position_already_active_for_setup"
        return meta

    preview = await preview_auto_paper_entry(
        session,
        setup=setup,
        validation=validation,
        structured=structured,
        symbol=symbol,
        require_ai_flags=require_ai_flags,
        live_price=live_price,
        ticker_time_ms=ticker_time_ms,
        now=clock,
    )
    meta.update({k: preview[k] for k in ("entry_mode", "order_type", "net_rr", "selection_audit", "rank", "live_price_assessment") if k in preview})
    if not preview.get("eligible"):
        meta["skipped"] = True
        meta["reason"] = preview.get("reason") or "preview_not_eligible"
        return meta

    from app.services.paper_opportunity_filters import persist_selection_audit

    selection_audit = preview["selection_audit"]
    persist_selection_audit(setup, selection_audit)
    order_type = str(preview["order_type"])
    mode_diag = preview.get("entry_mode") or {}
    net_rr_mode = float(preview["net_rr"])
    entry = float(setup.entry_price)  # type: ignore[arg-type]
    sl = float(setup.stop_loss)  # type: ignore[arg-type]
    tp = float(setup.take_profit)  # type: ignore[arg-type]
    direction = (setup.direction or "").upper()

    from app.services.paper_execution_budget import (
        rollback_allocate_auto_paper_budget,
        try_allocate_auto_paper_budget,
    )

    if await _active_position_count(session) >= runtime.max_simultaneous_positions:
        meta["skipped"] = True
        meta["reason"] = "max_simultaneous_positions"
        return meta

    budget_ok, budget_reason, budget_meta = await try_allocate_auto_paper_budget(session)
    meta["budget"] = budget_meta
    if not budget_ok:
        meta["skipped"] = True
        meta["reason"] = budget_reason
        return meta

    ict_snapshot = build_ict_entry_audit_snapshot(structured=structured, setup=setup, validation=validation)
    order_extra = {
        "auto_paper": True,
        "ai_run_id": run_id,
        "source": "gpt_auto_paper",
        "ict_entry_snapshot": ict_snapshot,
        "planned_entry": entry,
        "live_price_at_order": live_price,
        "distance_bps_at_order": mode_diag.get("distance_bps"),
        "selection_audit": selection_audit,
    }

    svc = SimPositionService(market=market or MarketDataService())
    req = OrderRequest(
        symbol=symbol,
        direction=direction,
        margin_usdt=settings.default_margin_usdt,
        leverage=settings.default_leverage,
        order_type=order_type,
        entry_price=entry if order_type == "limit" else None,
        stop_loss=sl,
        take_profit=tp,
        ai_setup_id=setup.setup_id,
        order_extra=order_extra,
        min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
    )
    try:
        result = await svc.create_order(session, req, commit=False)
    except PositionServiceError as exc:
        meta["skipped"] = True
        detail = str(exc)
        meta["detail"] = detail
        if detail.startswith("net_rr_below_min:"):
            meta["reason"] = "net_rr_below_min_at_execution_price"
            try:
                meta["net_rr"] = float(detail.split(":", 1)[1])
            except ValueError:
                pass
        else:
            meta["reason"] = "order_rejected"
        await rollback_allocate_auto_paper_budget(session)
        return meta

    if not result.success or not result.position_id:
        meta["skipped"] = True
        meta["reason"] = "order_failed"
        meta["detail"] = result.message
        await rollback_allocate_auto_paper_budget(session)
        return meta

    pos = await svc.get_position_by_public_id(session, result.position_id)
    if pos and pos.status == "open":
        await mark_setup_paper_fill(session, pos)

    meta["created"] = True
    meta["position_id"] = result.position_id
    meta["setup_id"] = setup.setup_id
    meta["order_type"] = order_type
    meta["net_rr_at_execution"] = net_rr_mode
    meta["ict_entry_snapshot"] = ict_snapshot
    logger.info(
        "PAPER AUTO ENTRY CREATED",
        extra={
            "position_id": result.position_id,
            "setup_id": setup.setup_id,
            "run_id": run_id,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
        },
    )

    if commit:
        await session.commit()
    return meta


async def auto_paper_entry_still_valid(session: AsyncSession, pos: SimPosition) -> tuple[bool, str | None]:
    if not (pos.extra or {}).get("auto_paper"):
        return True, None
    if not pos.ai_setup_id:
        return False, "missing_ai_setup_id"
    setup = (
        await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id))
    ).scalar_one_or_none()
    if setup is None:
        return False, "setup_not_found"
    lifecycle = (setup.lifecycle_state or "").upper()
    require_flags = lifecycle not in (READY_LONG, READY_SHORT, PENDING_ENTRY, POSITION_OPEN)
    return setup_eligible_for_auto_entry(setup, require_ai_flags=require_flags)


async def mark_setup_paper_fill(session: AsyncSession, pos: SimPosition) -> None:
    if not pos.ai_setup_id:
        return
    setup = (
        await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id))
    ).scalar_one_or_none()
    if setup:
        setup.paper_fill_recorded = True
        from app.domain.setup_lifecycle import POSITION_OPEN

        setup.lifecycle_state = POSITION_OPEN
