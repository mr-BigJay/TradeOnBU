"""AI-first *paper-only* market execution without ICT WAIT/confirmation sequences.

This is deliberately independent of the legacy verified conditional plan monitor.
Only model's direct closed-data recommendation can trigger it; no forced trade.
Account exposure limits, daily loss limit, 2.0 net R, risk budget and paper pause
remain hard protections. No Bitunix live-order adapter is available in this path.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.models import SimPosition
from app.services.execution_model_sim import auto_paper_rr_from_settings
from app.services.position.execution_adapters import OrderRequest
from app.services.position.position_service import PositionServiceError, SimPositionService
from app.services.position.settings_service import get_or_create_settings
from app.services.runtime_settings import get_runtime_settings
from app.services.verified_plan_risk import budget, risk_margin
from app.services.position.wallet_service import wallet_summary
from app.services.ict_execution_plan import target_at_net_rr

MIN_NET_RR = 2.0
MAX_REFERENCE_DRIFT_PCT = 0.50

DIRECT_INSTRUCTIONS = """
OPTIONAL AI-FIRST PAPER TRADE OUTPUT (independent of forecast_scenarios and plans):
Return an additional key named trade_now, one object with:
{"decision":"LONG"|"SHORT"|"NO_TRADE", "stop_loss":number|null,
 "take_profit":number|null, "reason_fa":"..."}
Analyze the supplied actual closed 4H/1H/15m/5m candles, indicators and major
structural levels, then make ONE discretionary decision for a market PAPER entry
near the CURRENT ticker, NOT a distant conditional pullback. Prefer trading when
a coherent direction, structural stop and realistic 2R or better target exist.
No sweep, MSS, FVG, OB touch, breakout/retest or EMA/RSI threshold is mandatory.
Do not force LONG/SHORT if stop and target cannot be responsibly derived.
Stop must be a numeric structural invalidation price on the adverse side;
take_profit must be a numeric plausible price beyond 2R of current market.
Use genuine supplied candle prices; do not invent levels or probabilities.
Explain the positive thesis and a clear failure point in Persian. Do not mark
any closed-candle confirmation as verified unless supported by input.
A direct model proposal is not an order: the SERVER checks current price drift,
geometry, fees, minimum 2.0 net R:R, account risk, and paper controls before entry.
The conditional plans and 4h outlook are separate, remain optional and watch-only.
"""


def enabled():
    return os.getenv("AI_DIRECT_PAPER_ENTRY_ENABLED", "0").strip() == "1"


def _num(raw):
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def preflight(proposal, *, reference_price, execution_price, settings):
    """Pure structural and fee-aware checks, WITHOUT ICT confirmation gates."""
    if not isinstance(proposal, dict):
        return None, "missing_direct_proposal"
    direction = proposal.get("decision")
    if direction == "NO_TRADE":
        return None, "ai_decided_no_trade"
    if direction not in ("LONG", "SHORT"):
        return None, "invalid_ai_direction"
    entry, stop, proposed_target = (
        _num(execution_price), _num(proposal.get("stop_loss")),
        _num(proposal.get("take_profit"))
    )
    reference = _num(reference_price)
    if not all((entry, stop, proposed_target, reference)):
        return None, "invalid_ai_levels"
    if abs(entry - reference) / reference * 100 > MAX_REFERENCE_DRIFT_PCT:
        return None, "price_moved_since_ai_analysis"
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - stop) <= 0 or sign * (proposed_target - entry) <= 0:
        return None, "invalid_ai_geometry"
    # Use the AI's proposed destination as an upper bound. Find the nearest
    # executable tenth-dollar tick reaching net 2R *after* Bitunix VIP fees.
    # The ordinary position service caps TP at 2.5R, so account for it here.
    max_tp = entry + sign * min(abs(proposed_target - entry), 2.5 * abs(entry - stop))
    final_tp = target_at_net_rr(
        settings, direction, entry, stop, max_tp, minimum=MIN_NET_RR
    )
    if final_tp is None:
        return None, "ai_target_below_two_net_r"
    rr = auto_paper_rr_from_settings(
        settings, direction=direction, entry=entry, sl=stop, tp=final_tp,
        min_net_rr=MIN_NET_RR, order_type="market"
    )
    # The general target_at_net_rr helper uses limit-order fees. Market
    # entries require taker fees; search the finite 0.1 tick range again.
    if not rr.acceptable:
        max_tick = int(math.floor(sign * max_tp * 10))
        start_tick = int(math.floor(sign * final_tp * 10))
        target = None
        for tick in range(start_tick, max_tick + 1):
            candidate = sign * tick / 10
            result = auto_paper_rr_from_settings(
                settings, direction=direction, entry=entry, sl=stop, tp=candidate,
                min_net_rr=MIN_NET_RR, order_type="market"
            )
            if result.acceptable:
                target = candidate
                rr = result
                break
        if target is None:
            return None, "market_fees_prevent_two_net_r"
        final_tp = target
    return {
        "direction": direction,
        "entry_reference": entry,
        "stop_loss": stop,
        "take_profit": final_tp,
        "ai_proposed_target": proposed_target,
        "net_rr_preflight": round(rr.net_rr, 3),
        "reason_fa": str(proposal.get("reason_fa") or "").strip()[:900],
    }, None


async def execute_direct_paper(session, *, run_id, proposal, reference_price,
                               current_price, symbol="BTCUSDT"):
    """Create at most one direct market PAPER position per AI run, never live."""
    result = {"enabled": enabled(), "attempted": True, "created": False, "run_id": run_id}
    if not enabled():
        return {**result, "reason": "direct_mode_disabled"}
    if symbol != "BTCUSDT":
        return {**result, "reason": "btc_only"}
    from app.services.entry_strategies import strategy_settings
    if not (await strategy_settings(session)).get("ict_reversal"):
        return {**result, "reason": "ict_reversal_strategy_disabled"}
    settings = await get_or_create_settings(session)
    runtime = await get_runtime_settings(session)
    if not settings.auto_open_paper_positions or runtime.paper_trading_paused or runtime.paper_entry_dry_run:
        return {**result, "reason": "paper_not_enabled_or_paused"}
    if not settings.initial_balance_set:
        return {**result, "reason": "wallet_not_initialized"}

    validated, reason = preflight(
        proposal, reference_price=reference_price,
        execution_price=current_price, settings=settings
    )
    if reason:
        return {**result, "reason": reason}
    result["proposal"] = validated

    # A double call for the same run must never re-open after a quick SL/TP.
    previous = (await session.execute(
        select(SimPosition).where(
            SimPosition.extra["ai_direct_paper"]["run_id"].as_integer() == run_id
        ).limit(1)
    )).scalar_one_or_none()
    if previous is not None:
        return {**result, "reason": "run_already_executed", "position_id": previous.public_id}

    # Reuse verified-plan daily loss / one-position protections, but do NOT
    # import its confirmation engine or require a conditional setup record.
    pseudo_setup = SimpleNamespace(
        direction=validated["direction"],
        entry_price=validated["entry_reference"],
        stop_loss=validated["stop_loss"]
    )
    risk, reason = await budget(session, settings, pseudo_setup, datetime.now(timezone.utc))
    if reason:
        return {**result, "reason": reason}

    # Market orders pay taker fees for entry; trim the margin conservatively.
    wallet = await wallet_summary(session)
    margin_market = risk_margin(
        equity=float(wallet["account_equity"]),
        available=float(wallet["available_balance"]),
        entry=validated["entry_reference"],
        stop=validated["stop_loss"],
        direction=validated["direction"],
        maker=settings.taker_fee_rate,
        taker=settings.taker_fee_rate,
        slippage_bps=settings.slippage_bps,
        leverage=risk["leverage"],
        cap=min(settings.default_margin_usdt, settings.max_margin_per_position),
    )
    margin = min(margin_market, risk["margin"])
    if margin <= 0:
        return {**result, "reason": "no_market_risk_budget"}

    # No exchange adapter or live key used. This opens a SIM market position.
    service = SimPositionService()
    request = OrderRequest(
        symbol=symbol,
        direction=validated["direction"],
        margin_usdt=margin,
        leverage=risk["leverage"],
        order_type="market",
        entry_price=None,
        stop_loss=validated["stop_loss"],
        take_profit=validated["take_profit"],
        ai_setup_id=None,
        order_extra={
            "auto_paper": True,
            "ai_direct_paper": {
                "run_id": run_id,
                "reason_fa": validated["reason_fa"],
                "entry_reference": validated["entry_reference"],
                "net_rr_preflight": validated["net_rr_preflight"],
                "paper_only": True,
            },
        },
    )
    try:
        async with session.begin_nested():
            created = await service.create_order(session, request, commit=False)
            if not created.success:
                return {**result, "reason": "market_fill_rejected", "detail": created.message}
    except PositionServiceError as exc:
        return {**result, "reason": "paper_order_rejected", "detail": str(exc)[:180]}
    return {
        **result, "created": True, "position_id": created.position_id,
        "status": created.payload.get("status"),
        **validated,
    }
