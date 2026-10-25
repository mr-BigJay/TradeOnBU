"""Unified confirmation policy: required vs optional vs setup-type minimums."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.conditional_setup import CONFIRMATION_PROFILES
from app.services.conditional_revision import entry_zone_touch_evidence_valid
from app.services.ict_confirmation_engine import sequence_order_valid

# Structural tokens that must not be dropped by AI optional/required hints alone.
PROFILE_MIN_STRUCTURAL: dict[str, tuple[str, ...]] = {
    "trend_pullback_resume": ("trend_pullback", "trend_resume"),
    "level_breakout_retest": ("breakout_close", "breakout_retest"),
    "sweep_mss_poi": ("liquidity_sweep", "mss"),
    "sweep_bos_poi": ("liquidity_sweep", "bos"),
    "bos_fvg_retest": ("bos", "fvg_retest"),
    "mss_fvg_retest": ("mss", "fvg_retest"),
}


@dataclass(frozen=True)
class ConfirmationPolicy:
    """Single source for promotion and order gates."""

    sequence: tuple[str, ...]
    required: tuple[str, ...]
    optional: tuple[str, ...]
    pre_entry_required: tuple[str, ...]
    confirmation_type: str | None

    def as_state_fields(self) -> dict[str, Any]:
        return {
            "sequence": list(self.sequence),
            "required": list(self.required),
            "optional": list(self.optional),
            "pre_entry_required": list(self.pre_entry_required),
            "confirmation_type": self.confirmation_type,
        }


def resolve_confirmation_policy(
    *,
    sequence: list[str],
    structured: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> ConfirmationPolicy:
    structured = structured or {}
    plan = plan or {}
    seq = tuple(str(x) for x in sequence)
    ctype = structured.get("confirmation_type") or plan.get("confirmation_type")
    if isinstance(ctype, str):
        ctype = ctype.strip() or None
    else:
        ctype = None

    min_struct = PROFILE_MIN_STRUCTURAL.get(ctype or "", ())
    if not min_struct:
        for profile_seq in CONFIRMATION_PROFILES.values():
            if tuple(profile_seq) == seq:
                min_struct = tuple(t for t in profile_seq if t != "entry_zone_touch")
                break

    optional_raw = structured.get("optional_confirmations") or plan.get("optional_confirmations") or []
    optional: set[str] = set()
    if isinstance(optional_raw, list):
        optional = {str(x) for x in optional_raw if str(x) in seq}

    # AI optional hints cannot drop profile minimums or final entry proof token.
    for t in min_struct:
        optional.discard(t)
    if "entry_zone_touch" in seq:
        optional.discard("entry_zone_touch")

    required_set = {t for t in seq if t not in optional}
    for t in min_struct:
        if t in seq:
            required_set.add(t)

    ai_required_raw = structured.get("required_confirmations") or plan.get("required_confirmations")
    if isinstance(ai_required_raw, list) and ai_required_raw:
        for t in (str(x) for x in ai_required_raw):
            if t in seq:
                required_set.add(t)

    required = tuple(t for t in seq if t in required_set)
    optional_tuple = tuple(t for t in seq if t in optional and t not in required_set)
    pre_entry = tuple(t for t in required if t != "entry_zone_touch")

    return ConfirmationPolicy(
        sequence=seq,
        required=required,
        optional=optional_tuple,
        pre_entry_required=pre_entry,
        confirmation_type=ctype,
    )


def _timestamps(state: dict[str, Any]) -> dict[str, int]:
    return dict(state.get("confirmation_timestamps") or {})


def tokens_met(tokens: tuple[str, ...], state: dict[str, Any]) -> bool:
    ts = _timestamps(state)
    if not all(t in ts for t in tokens):
        return False
    return sequence_order_valid(list(tokens), ts)


def promotion_ready(
    policy: ConfirmationPolicy,
    state: dict[str, Any],
    *,
    allow_pre_entry: bool = False,
) -> bool:
    if state.get("invalidated"):
        return False
    if tokens_met(policy.required, state):
        return True
    if allow_pre_entry and policy.pre_entry_required:
        return tokens_met(policy.pre_entry_required, state)
    return False


def order_entry_allowed(
    policy: ConfirmationPolicy,
    state: dict[str, Any],
    *,
    allow_pre_entry_without_touch: bool,
    zone_lo: float | None,
    zone_hi: float | None,
) -> tuple[bool, str | None]:
    ts = _timestamps(state)
    if allow_pre_entry_without_touch:
        if not tokens_met(policy.pre_entry_required, state):
            return False, "missing_confirmations"
        return True, None

    if not tokens_met(policy.required, state):
        return False, "missing_confirmations"

    if "entry_zone_touch" in policy.required and "entry_zone_touch" in ts:
        if zone_lo is None or zone_hi is None:
            return False, "missing_confirmations"
        touch_ev = (state.get("confirmation_evidence") or {}).get("entry_zone_touch")
        if not entry_zone_touch_evidence_valid(
            touch_ev, zone_lo=float(zone_lo), zone_hi=float(zone_hi)
        ):
            return False, "missing_confirmations"
    return True, None
