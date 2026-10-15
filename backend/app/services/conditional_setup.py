from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.setup_lifecycle import SETUP_MODE_CONDITIONAL, wait_state_for_direction
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.services.ai_trader_report import _has_conditional_plan

MIN_EXPIRES_HOURS = 1.0
MAX_EXPIRES_HOURS = 168.0

ALLOWED_CONFIRMATION_TOKENS = frozenset(
    {
        "liquidity_sweep",
        "mss",
        "bos",
        "fvg_retest",
        "order_block_retest",
        "entry_zone_touch",
        "displacement",
    }
)

CONFIRMATION_PROFILES: dict[str, list[str]] = {
    "sweep_mss_poi": ["liquidity_sweep", "mss", "entry_zone_touch"],
    "sweep_bos_poi": ["liquidity_sweep", "bos", "entry_zone_touch"],
    "bos_fvg_retest": ["bos", "fvg_retest", "entry_zone_touch"],
    "mss_fvg_retest": ["mss", "fvg_retest", "entry_zone_touch"],
}


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
        if f != f or f <= 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def resolve_confirmation_sequence(structured: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    """Fail closed: require confirmation_type profile or explicit confirmation_sequence array."""
    ctype = structured.get("confirmation_type")
    if isinstance(ctype, str) and ctype in CONFIRMATION_PROFILES:
        return list(CONFIRMATION_PROFILES[ctype]), None

    seq = structured.get("confirmation_sequence")
    if isinstance(seq, list) and len(seq) >= 2:
        tokens = [str(x).strip() for x in seq]
        if all(t in ALLOWED_CONFIRMATION_TOKENS for t in tokens):
            return tokens, None
        return None, "unsupported_confirmation_sequence_token"

    return None, "missing_structured_confirmation_type"


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

    sl = _parse_float(structured.get("proposed_stop_loss") or structured.get("stop_loss"))
    tp = _parse_float(structured.get("proposed_take_profit") or structured.get("take_profit"))
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
    if inv_level is None:
        return None, "missing_structural_invalidation_level"

    sequence, seq_err = resolve_confirmation_sequence(structured)
    if not sequence:
        return None, seq_err or "missing_confirmation_sequence"

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
        "confirmation_type": structured.get("confirmation_type"),
        "confirmation_sequence": sequence,
        "structural_invalidation_level": inv_level,
        "proposed_stop_loss": sl,
        "proposed_take_profit": tp,
        "proposed_entry_mid": entry_mid,
        "confirmation_timeframe": structured.get("confirmation_timeframe") or "5m",
    }
    plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
    return plan, None


def apply_conditional_plan_to_setup_row(
    row: Any, *, plan: dict[str, Any], run_id: int, structured: dict[str, Any], created_at: datetime | None = None
) -> None:
    direction = plan["potential_direction"]
    sequence = plan["confirmation_sequence"]
    created_at = created_at or datetime.now(timezone.utc)
    boundary = first_eligible_bar_open_ms(created_at, "5m")

    row.direction = direction
    row.setup_mode = SETUP_MODE_CONDITIONAL
    row.final_ai_decision = "WAIT"
    row.lifecycle_state = wait_state_for_direction(direction)
    row.monitoring_enabled = True
    row.monitor_from_ms = boundary
    row.monitor_cursor_ms = boundary - 1
    row.entry_zone_low = plan["entry_zone_low"]
    row.entry_zone_high = plan["entry_zone_high"]
    row.entry_price = plan["proposed_entry_mid"]
    row.stop_loss = plan["proposed_stop_loss"]
    row.take_profit = plan["proposed_take_profit"]
    row.invalidation_level = plan.get("structural_invalidation_level")
    row.invalidation = structured.get("conditional_invalidation") or structured.get("invalidation_condition")
    row.conditional_plan = plan
    row.confirmation_state = {
        "sequence": list(sequence),
        "required": list(sequence),
        "completed": [],
        "missing": list(sequence),
        "confirmation_timestamps": {},
    }
    row.run_id = run_id
    row.structured_snapshot = structured


def resolve_immediate_confirmation_sequence(structured: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    return resolve_confirmation_sequence(structured)
