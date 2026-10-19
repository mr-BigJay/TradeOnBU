"""BALANCED ICT confirmation: 1m granularity within closed 5m structure (no look-ahead)."""

from __future__ import annotations

from typing import Any

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms
from app.services.confirmation_replay import _ict_window_at_1m_close
from app.services.ict_confirmation_engine import (
    ConfirmationProgress,
    _check_invalidation_on_bar,
    _token_on_bar,
    next_sequence_token,
    process_bar_sequential,
    sequence_order_valid,
)

_ICT_MIN_WINDOW_BARS = 13
_TOUCH_TOKENS = frozenset({"entry_zone_touch", "order_block_retest"})


def effective_required_tokens(
    sequence: list[str],
    structured: dict[str, Any] | None,
) -> list[str]:
    """Setup-tailored required set; defaults to full approved sequence."""
    structured = structured or {}
    req = structured.get("required_confirmations")
    if isinstance(req, list) and req:
        filtered = [str(x) for x in req if str(x) in sequence]
        if filtered:
            return filtered
    return list(sequence)


def pre_entry_tokens(sequence: list[str]) -> list[str]:
    return [t for t in sequence if t != "entry_zone_touch"]


def pre_entry_requirements_met(
    *,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    structured: dict[str, Any] | None = None,
) -> bool:
    required = effective_required_tokens(sequence, structured)
    pre = [t for t in required if t != "entry_zone_touch"]
    if not pre:
        return False
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    if not all(t in timestamps for t in pre):
        return False
    return sequence_order_valid(pre, timestamps)


def required_confirmations_met(
    *,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    structured: dict[str, Any] | None = None,
) -> bool:
    required = effective_required_tokens(sequence, structured)
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    if not all(t in timestamps for t in required):
        return False
    return sequence_order_valid(required, timestamps)


def _closed_1m_in_5m_window(
    *,
    bar_5m: Candle,
    closed_1m: list[Candle],
    now_ms: int,
) -> list[Candle]:
    close_5m = candle_close_ms(bar_5m, "5m")
    out: list[Candle] = []
    for c in sorted(closed_1m, key=lambda x: x.time_ms):
        if c.time_ms < bar_5m.time_ms or c.time_ms >= close_5m:
            continue
        if now_ms < candle_close_ms(c, "1m"):
            continue
        out.append(c)
    return out


def _try_token_on_1m(
    *,
    token: str,
    direction: str,
    bar_1m: Candle,
    bar_5m: Candle,
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
) -> bool:
    if token in _TOUCH_TOKENS:
        window = [bar_1m]
        bar = bar_1m
    else:
        window = _ict_window_at_1m_close(
            closed_5m=closed_5m,
            bar_5m=bar_5m,
            closed_1m=closed_1m,
            eval_1m_ms=bar_1m.time_ms,
        )
        if len(window) < _ICT_MIN_WINDOW_BARS:
            return False
        bar = window[-1]
    return _token_on_bar(
        token,
        direction=direction,
        window=window,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
    )


def advance_balanced_on_closed_5m_bar(
    *,
    direction: str,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    bar_5m: Candle,
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
    monitor_from_ms: int,
    now_ms: int,
) -> ConfirmationProgress:
    """
    Advance confirmations using closed 1m bars inside this 5m window, then fall back to
    closed 5m sequential step (same as STRICT) for any remaining token on the 5m close.
    """
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    evidence = dict(confirmation_state.get("confirmation_evidence") or {})
    completed = [t for t in sequence if t in timestamps]

    progress = ConfirmationProgress(
        sequence=list(sequence),
        completed=completed,
        missing=[t for t in sequence if t not in completed],
        confirmation_timestamps=timestamps,
        confirmation_evidence=evidence,
        last_bar_ms=bar_5m.time_ms,
    )

    if bar_5m.time_ms < monitor_from_ms:
        return progress

    if _check_invalidation_on_bar(
        bar_5m, direction=direction, invalidation_level=invalidation_level
    ):
        progress.invalidated = True
        progress.invalidation_reason = "structural_invalidation_level_breached"
        return progress

    for bar_1m in _closed_1m_in_5m_window(bar_5m=bar_5m, closed_1m=closed_1m, now_ms=now_ms):
        nxt = next_sequence_token(sequence, [t for t in sequence if t in timestamps])
        if nxt is None:
            break
        if _try_token_on_1m(
            token=nxt,
            direction=direction,
            bar_1m=bar_1m,
            bar_5m=bar_5m,
            closed_5m=closed_5m,
            closed_1m=closed_1m,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
        ):
            timestamps[nxt] = bar_1m.time_ms
            if nxt == "entry_zone_touch":
                evidence[nxt] = {
                    "bar_low": float(bar_1m.low),
                    "bar_high": float(bar_1m.high),
                    "zone_lo": float(zone_lo),
                    "zone_hi": float(zone_hi),
                    "granularity": "1m",
                }
            progress.confirmation_timestamps = timestamps
            progress.confirmation_evidence = evidence
            progress.completed = [t for t in sequence if t in timestamps]
            progress.missing = [t for t in sequence if t not in timestamps]

    # Closed 5m fallback (identical to STRICT for the final token if not yet seen on 1m)
    strict_step = process_bar_sequential(
        direction=direction,
        sequence=sequence,
        confirmation_state={
            "confirmation_timestamps": progress.confirmation_timestamps,
            "confirmation_evidence": progress.confirmation_evidence,
        },
        bar=bar_5m,
        window=[c for c in closed_5m if c.time_ms <= bar_5m.time_ms][-48:],
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        invalidation_level=invalidation_level,
        structured=structured,
        monitor_from_ms=monitor_from_ms,
    )
    if strict_step.invalidated:
        return strict_step

    progress.confirmation_timestamps = strict_step.confirmation_timestamps
    progress.confirmation_evidence = strict_step.confirmation_evidence
    progress.completed = strict_step.completed
    progress.missing = strict_step.missing
    progress.last_bar_ms = bar_5m.time_ms
    return progress


def confirmation_progress_from_state(
    sequence: list[str],
    confirmation_state: dict[str, Any],
) -> ConfirmationProgress:
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    completed = [t for t in sequence if t in timestamps]
    return ConfirmationProgress(
        sequence=list(sequence),
        completed=completed,
        missing=[t for t in sequence if t not in completed],
        confirmation_timestamps=timestamps,
        confirmation_evidence=dict(confirmation_state.get("confirmation_evidence") or {}),
    )


def balanced_all_required_met(
    *,
    sequence: list[str],
    confirmation_state: dict[str, Any],
    structured: dict[str, Any] | None,
) -> bool:
    if confirmation_state.get("invalidated"):
        return False
    return required_confirmations_met(
        sequence=sequence,
        confirmation_state=confirmation_state,
        structured=structured,
    )
