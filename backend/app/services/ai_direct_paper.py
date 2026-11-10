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
REQUIRED AI-FIRST PAPER TRADE DECISION IN EVERY RESPONSE:
Add a top-level trade_now object alongside the existing market_summary_fa,
comparison_fa, forecast_scenarios, and plans. Never omit trade_now:
{"decision":"LONG"|"SHORT"|"NO_TRADE",
 "stop_loss":number|null,"take_profit":number|null,"reason_fa":"Persian rationale"}.
This is an INDEPENDENT immediate MARKET Paper entry assessment, NOT one of
the mechanically confirmed conditional 'plans'. It can select LONG/SHORT even
if plans=[] or the 4H structural swing regime is range. The legacy plan rules
do not impose Sweep, MSS, FVG, OB-touch, retest, EMA, RSI or a continuation
regime gate on trade_now. Those are optional evidence, not mandatory tokens.

First evaluate a LONG and a SHORT at market.live_price against all supplied
closed 4H/1H/15m/5m candles, 1H/4H major areas, 5m/15m structure, indicators,
and fresh market_intelligence. Decide which direction (if any) has the most
credible entry at the CURRENT price, with a clear structural invalidation and
room to an evidence-based target. Do not reject merely because the market has
a range label, plans were rejected or a full ICT sequence did not appear.
Do not sell directly into a nearby support or buy directly into resistance;
use all supplied candlestick data to distinguish an opportunity from chase.
Prefer an actionable trade when price location, structural stop, and
target room align; NO_TRADE is acceptable when neither side has a responsible
structure/target. Never fabricate a setup or promise its outcome.

For LONG/SHORT, supply real numeric stop_loss (beyond invalidation) and a
realistic numeric take_profit (beyond the required NET 2R, not just GROSS 2R).
Use the 'direct_paper_execution' block supplied in the user JSON: Bitunix
maker/taker fees, adverse slip assumption, maximum gross TP cap of 2.5R,
latest entry reference price, and required 2.0 NET R:R. Account for BOTH
entry and exit costs and the loss-side slippage. The target must be
plausible from the supplied actual candles, ideally before a nearby major
obstacle. An imagined extension past a known strong obstacle is not a
valid target. If there is no valid room, say NO_TRADE and identify the
specific obstacle/limiting price in reason_fa. If direct_paper_execution
is absent, do not invent fee or execution settings.

All trade_now reasons must be Persian and explain directional evidence
and exact failure price. The server obtains a fresh ticker after inference
and may veto for drift, fees, net R:R, daily risk, existing position or pause.
This mode is Paper-only; never claim a live Bitunix trade was placed.
The four-hour outlook and conditional plans are separate, watch-only outputs.
"""


def execution_context(settings, *, reference_price):
    """Costs and limits visible to the model before its market-entry proposal."""
    from app.domain.execution_tp_cap import EXECUTION_MAX_REWARD_R_MULTIPLE

    return {
        "mode": "paper_market_only",
        "reference_price": float(reference_price),
        "entry_fee_rate": float(settings.taker_fee_rate),
        "stop_fee_rate": float(settings.taker_fee_rate),
        "tp_fee_rate": float(
            settings.maker_fee_rate if settings.default_tp_behavior == "limit"
            else settings.taker_fee_rate
        ),
        "slippage_bps": float(settings.slippage_bps),
        "minimum_net_rr": MIN_NET_RR,
        "execution_max_gross_rr": EXECUTION_MAX_REWARD_R_MULTIPLE,
        "reference_drift_max_pct": MAX_REFERENCE_DRIFT_PCT,
        "net_rr_definition": (
            "Net profit at TP after fees / loss at SL after entry/stop fees "
            "and adverse stop slippage; 2.0 gross is usually insufficient"
        ),
        "advice": (
            "Evaluate 5m/15m and HTF location, choose a structural SL and "
            "a credible real-candle target with at least 2.0 net R:R. "
            "If no nearby target fits, choose NO_TRADE, not an invented extension."
        ),
    }



def enabled():
    return os.getenv("AI_DIRECT_PAPER_ENTRY_ENABLED", "0").strip() == "1"


def _num(raw):
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def rejection_diagnostics(proposal, *, execution_price, settings):
    """Explain fee/cap rejections without changing the model's trade prices."""
    if not isinstance(proposal, dict):
        return {}
    direction = proposal.get("decision")
    entry = _num(execution_price)
    stop = _num(proposal.get("stop_loss"))
    target = _num(proposal.get("take_profit"))
    if direction not in ("LONG", "SHORT") or not all((entry, stop, target)):
        return {}
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - stop) <= 0 or sign * (target - entry) <= 0:
        return {}
    from app.domain.execution_tp_cap import EXECUTION_MAX_REWARD_R_MULTIPLE
    risk = abs(entry - stop)
    gross_rr = sign * (target - entry) / risk
    capped_target = entry + sign * EXECUTION_MAX_REWARD_R_MULTIPLE * risk
    raw_result = auto_paper_rr_from_settings(
        settings, direction=direction, entry=entry, sl=stop, tp=target,
        min_net_rr=MIN_NET_RR, order_type="market"
    )
    capped_result = auto_paper_rr_from_settings(
        settings, direction=direction, entry=entry, sl=stop, tp=capped_target,
        min_net_rr=MIN_NET_RR, order_type="market"
    )
    return {
        "reference_entry": round(entry, 2),
        "ai_target_gross_rr": round(gross_rr, 3),
        "ai_target_executable_net_rr": round(raw_result.net_rr, 3),
        "net_rr_at_execution_tp_cap": round(capped_result.net_rr, 3),
        "required_net_rr": MIN_NET_RR,
        "execution_tp_cap_gross_rr": EXECUTION_MAX_REWARD_R_MULTIPLE,
    }


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
        result["reason"] = reason
        if reason in ("ai_target_below_two_net_r", "market_fees_prevent_two_net_r"):
            result["risk_diagnostic"] = rejection_diagnostics(
                proposal, execution_price=current_price, settings=settings
            )
        return result
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
