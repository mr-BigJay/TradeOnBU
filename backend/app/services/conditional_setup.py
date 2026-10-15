from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.setup_lifecycle import SETUP_MODE_CONDITIONAL, wait_state_for_direction
from app.services.ai_trader_report import _has_conditional_plan

MIN_EXPIRES_HOURS = 1.0
MAX_EXPIRES_HOURS = 168.0


def _validate_levels(direction: str, entry: float, sl: float, tp: float) -> tuple[bool, str | None]:
    direction = direction.upper()
    if direction == "LONG":
        if not (sl < entry < tp):
            return False, "long_levels_not_ordered_sl_entry_tp"
    elif direction == "SHORT":
        if not (tp < entry < sl):
            return False, "short_levels_not_ordered_tp_entry_sl"
    else:
        return False, "invalid_direction"
    return True, None


def _parse_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        if f != f or f <= 0:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def normalize_confirmation_tokens(text: str) -> list[str]:
    """Map free-text confirmation hints to objective monitor tokens."""
    t = text.lower()
    tokens: list[str] = []
    if re.search(r"\bmss\b|market structure shift", t):
        tokens.append("mss")
    if re.search(r"\bbos\b|break of structure", t):
        tokens.append("bos")
    if re.search(r"\bfvg\b|fair value gap", t):
        tokens.append("fvg_retest")
    if re.search(r"\bob\b|order block", t):
        tokens.append("order_block_retest")
    if re.search(r"sweep|liquidity", t):
        tokens.append("liquidity_sweep")
    if re.search(r"displacement", t):
        tokens.append("displacement")
    if re.search(r"poi|point of interest|retest", t):
        tokens.append("entry_zone_touch")
    if not tokens:
        tokens = ["entry_zone_touch", "liquidity_sweep", "mss"]
    if "entry_zone_touch" not in tokens:
        tokens.insert(0, "entry_zone_touch")
    # preserve order, unique
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def parse_conditional_plan(structured: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not _has_conditional_plan(structured):
        return None, "not_conditional_plan"

    direction = str(
        structured.get("potential_direction")
        or structured.get("market_bias")
        or ""
    ).upper()
    if direction not in ("LONG", "SHORT"):
        return None, "missing_potential_direction"

    lo = _parse_float(structured.get("conditional_entry_zone_low") or structured.get("entry_zone_low"))
    hi = _parse_float(structured.get("conditional_entry_zone_high") or structured.get("entry_zone_high"))
    if lo is None or hi is None or lo >= hi:
        return None, "invalid_entry_zone"

    sl = _parse_float(
        structured.get("proposed_stop_loss")
        or structured.get("stop_loss")
    )
    tp = _parse_float(
        structured.get("proposed_take_profit")
        or structured.get("take_profit")
    )
    if sl is None or tp is None:
        return None, "missing_proposed_levels"

    entry_mid = (lo + hi) / 2.0
    ok, err = _validate_levels(direction, entry_mid, sl, tp)
    if not ok:
        return None, err

    inv_level = _parse_float(
        structured.get("structural_invalidation_level")
        or structured.get("invalidation_level")
    )
    conf_text = str(
        structured.get("confirmation_required")
        or structured.get("confirmation_conditions")
        or ""
    )
    required = normalize_confirmation_tokens(conf_text)

    expires_hours = structured.get("expires_hours") or 48
    try:
        expires_hours = float(expires_hours)
    except (TypeError, ValueError):
        expires_hours = 48.0
    expires_hours = max(MIN_EXPIRES_HOURS, min(MAX_EXPIRES_HOURS, expires_hours))

    plan: dict[str, Any] = {
        "potential_direction": direction,
        "setup_timeframe": structured.get("setup_timeframe") or structured.get("confirmation_timeframe"),
        "entry_zone_low": lo,
        "entry_zone_high": hi,
        "liquidity_reference": structured.get("liquidity_reference"),
        "liquidity_sweep_requirement": structured.get("liquidity_sweep_requirement"),
        "poi_type": structured.get("entry_poi_type") or structured.get("poi_type"),
        "poi_zone": {"low": lo, "high": hi},
        "confirmation_type": structured.get("confirmation_type"),
        "confirmation_timeframe": structured.get("confirmation_timeframe") or "5m",
        "confirmation_conditions": conf_text,
        "required_confirmations": required,
        "structural_invalidation_level": inv_level,
        "proposed_stop_loss": sl,
        "proposed_take_profit": tp,
        "proposed_entry_mid": entry_mid,
        "market_bias": structured.get("market_bias"),
        "risk_reward_estimate": structured.get("risk_reward"),
    }
    plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
    return plan, None


def apply_conditional_plan_to_setup_row(row: Any, *, plan: dict[str, Any], run_id: int, structured: dict[str, Any]) -> None:
    direction = plan["potential_direction"]
    row.direction = direction
    row.setup_mode = SETUP_MODE_CONDITIONAL
    row.final_ai_decision = "WAIT"
    row.lifecycle_state = wait_state_for_direction(direction)
    row.entry_zone_low = plan["entry_zone_low"]
    row.entry_zone_high = plan["entry_zone_high"]
    row.entry_price = plan["proposed_entry_mid"]
    row.stop_loss = plan["proposed_stop_loss"]
    row.take_profit = plan["proposed_take_profit"]
    row.invalidation_level = plan.get("structural_invalidation_level")
    row.invalidation = structured.get("conditional_invalidation") or structured.get("invalidation_condition")
    row.conditional_plan = plan
    row.confirmation_state = {
        "required": list(plan["required_confirmations"]),
        "completed": [],
        "missing": list(plan["required_confirmations"]),
    }
    row.run_id = run_id
    row.structured_snapshot = structured
