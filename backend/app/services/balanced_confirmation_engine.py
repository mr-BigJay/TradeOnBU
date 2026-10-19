"""BALANCED ICT confirmation on closed 1m bars (5m structure context, no look-ahead)."""

from __future__ import annotations

from typing import Any

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms, interval_ms
from app.services.confirmation_policy import ConfirmationPolicy
from app.services.confirmation_replay import (
    _ict_window_at_1m_close,
    _tokens_true_on_bar,
    chronology_provable_on_1m,
)
from app.services.ict_confirmation_engine import (
    ConfirmationProgress,
    _check_invalidation_on_bar,
    _token_on_bar,
    next_sequence_token,
    sequence_order_valid,
)

_ICT_MIN_WINDOW_BARS = 13
_TOUCH_TOKENS = frozenset({"entry_zone_touch", "order_block_retest"})
STEP_5M = interval_ms("5m")


def _parent_5m_open_ms(bar_1m_ms: int) -> int:
    return (bar_1m_ms // STEP_5M) * STEP_5M


def _closed_1m_bars_in_range(
    *,
    closed_1m: list[Candle],
    from_ms: int,
    to_ms: int,
    now_ms: int,
) -> list[Candle]:
    out: list[Candle] = []
    for c in sorted(closed_1m, key=lambda x: x.time_ms):
        if c.time_ms <= from_ms:
            continue
        if c.time_ms > to_ms:
            break
        if now_ms < candle_close_ms(c, "1m"):
            continue
        out.append(c)
    return out


def _1m_coverage_complete_for_5m(
    *,
    bar_5m: Candle,
    closed_1m: list[Candle],
    now_ms: int,
) -> bool:
    step1 = interval_ms("1m")
    close_5m = candle_close_ms(bar_5m, "5m")
    expected = [bar_5m.time_ms + i * step1 for i in range(5)]
    slot_map = {c.time_ms: c for c in closed_1m if bar_5m.time_ms <= c.time_ms < close_5m}
    for open_ms in expected:
        c = slot_map.get(open_ms)
        if c is None:
            return False
        if now_ms < candle_close_ms(c, "1m"):
            return False
    return True


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
    else:
        window = _ict_window_at_1m_close(
            closed_5m=closed_5m,
            bar_5m=bar_5m,
            closed_1m=closed_1m,
            eval_1m_ms=bar_1m.time_ms,
        )
        if len(window) < _ICT_MIN_WINDOW_BARS:
            return False
    return _token_on_bar(
        token,
        direction=direction,
        window=window,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
    )


def _would_violate_chronology_if_stamped(
    *,
    direction: str,
    sequence: list[str],
    timestamps: dict[str, int],
    new_token: str,
    new_ms: int,
    bar_5m: Candle,
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
    now_ms: int,
) -> bool:
    """True when stamping would create unprovable or out-of-order multi-token 5m window."""
    trial = dict(timestamps)
    trial[new_token] = new_ms
    ordered = [t for t in sequence if t in trial]
    if len(ordered) < 2:
        return False
    if not sequence_order_valid(ordered, trial):
        return True

    same_5m = [t for t in ordered if _parent_5m_open_ms(trial[t]) == bar_5m.time_ms]
    if len(same_5m) < 2:
        return False

    # Two tokens in one 5m window: strictly increasing closed 1m opens are enough.
    if len(same_5m) == 2:
        t1, t2 = same_5m[0], same_5m[1]
        return not (trial[t1] < trial[t2])

    # Three or more in the same 5m window require full 1m slot coverage and replay proof.
    if not _1m_coverage_complete_for_5m(bar_5m=bar_5m, closed_1m=closed_1m, now_ms=now_ms):
        return True

    window_5m = [c for c in closed_5m if c.time_ms <= bar_5m.time_ms][-48:]
    hits = _tokens_true_on_bar(
        direction=direction,
        sequence=sequence,
        bar=bar_5m,
        window=window_5m,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
    )
    hits_in_seq = [t for t in sequence if t in hits]
    if len(hits_in_seq) < 3:
        return False

    ok, _reason = chronology_provable_on_1m(
        direction=direction,
        sequence=sequence,
        hits_on_5m=hits_in_seq,
        bar_5m=bar_5m,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
        now_ms=now_ms,
    )
    return not ok


def process_balanced_new_closed_1m_bars(
    *,
    direction: str,
    policy: ConfirmationPolicy,
    confirmation_state: dict[str, Any],
    new_1m_bars: list[Candle],
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
    Advance confirmations on newly closed 1m bars (including inside an open 5m candle).
    No 5m-close wait for promotion; 5m fallback is not used here.
    """
    sequence = list(policy.sequence)
    timestamps = dict(confirmation_state.get("confirmation_timestamps") or {})
    evidence = dict(confirmation_state.get("confirmation_evidence") or {})
    invalidated = bool(confirmation_state.get("invalidated"))
    inv_reason = confirmation_state.get("invalidation_reason")

    last_1m_ms = int(confirmation_state.get("monitor_1m_cursor_ms") or monitor_from_ms - 1)

    progress = ConfirmationProgress(
        sequence=sequence,
        completed=[t for t in sequence if t in timestamps],
        missing=[t for t in sequence if t not in timestamps],
        confirmation_timestamps=timestamps,
        confirmation_evidence=evidence,
        last_bar_ms=last_1m_ms,
    )

    if invalidated:
        progress.invalidated = True
        progress.invalidation_reason = inv_reason
        return progress

    for bar_1m in sorted(new_1m_bars, key=lambda c: c.time_ms):
        if bar_1m.time_ms < monitor_from_ms:
            continue

        if _check_invalidation_on_bar(
            bar_1m, direction=direction, invalidation_level=invalidation_level
        ):
            progress.invalidated = True
            progress.invalidation_reason = "structural_invalidation_level_breached"
            progress.last_bar_ms = bar_1m.time_ms
            return progress

        parent_open = _parent_5m_open_ms(bar_1m.time_ms)
        bar_5m = next((b for b in closed_5m if b.time_ms == parent_open), None)
        if bar_5m is None:
            # Synthesize placeholder 5m bucket for ICT window (only closed 1m inside).
            partial = [c for c in closed_1m if parent_open <= c.time_ms <= bar_1m.time_ms]
            if not partial:
                last_1m_ms = bar_1m.time_ms
                continue
            bar_5m = Candle(
                time_ms=parent_open,
                open=partial[0].open,
                high=max(c.high for c in partial),
                low=min(c.low for c in partial),
                close=partial[-1].close,
                volume=sum(c.volume or 0 for c in partial),
            )

        nxt = next_sequence_token(sequence, [t for t in sequence if t in timestamps])
        if nxt is None:
            last_1m_ms = bar_1m.time_ms
            continue

        if not _try_token_on_1m(
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
            last_1m_ms = bar_1m.time_ms
            continue

        if _would_violate_chronology_if_stamped(
            direction=direction,
            sequence=sequence,
            timestamps=timestamps,
            new_token=nxt,
            new_ms=bar_1m.time_ms,
            bar_5m=bar_5m,
            closed_5m=closed_5m,
            closed_1m=closed_1m,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
            now_ms=now_ms,
        ):
            last_1m_ms = bar_1m.time_ms
            continue

        timestamps[nxt] = bar_1m.time_ms
        if nxt == "entry_zone_touch":
            evidence[nxt] = {
                "bar_low": float(bar_1m.low),
                "bar_high": float(bar_1m.high),
                "zone_lo": float(zone_lo),
                "zone_hi": float(zone_hi),
                "granularity": "1m",
                "engine": "BALANCED",
            }
        else:
            evidence[nxt] = {
                **(evidence.get(nxt) or {}),
                "granularity": "1m",
                "engine": "BALANCED",
                "bar_1m_ms": bar_1m.time_ms,
            }
        progress.confirmation_timestamps = timestamps
        progress.confirmation_evidence = evidence
        progress.completed = [t for t in sequence if t in timestamps]
        progress.missing = [t for t in sequence if t not in timestamps]
        last_1m_ms = bar_1m.time_ms

    progress.last_bar_ms = last_1m_ms
    return progress


def apply_balanced_progress_to_state(
    conf_state: dict[str, Any],
    progress: ConfirmationProgress,
    *,
    policy: ConfirmationPolicy,
    monitor_1m_cursor_ms: int,
) -> dict[str, Any]:
    out = {
        **conf_state,
        **policy.as_state_fields(),
        "completed": progress.completed,
        "missing": progress.missing,
        "confirmation_timestamps": progress.confirmation_timestamps,
        "confirmation_evidence": progress.confirmation_evidence,
        "monitor_1m_cursor_ms": monitor_1m_cursor_ms,
        "confirmation_engine": "BALANCED",
    }
    if progress.invalidated:
        out["invalidated"] = True
        out["invalidation_reason"] = progress.invalidation_reason
    return out


def strict_apply_5m_bar(
    *,
    direction: str,
    policy: ConfirmationPolicy,
    confirmation_state: dict[str, Any],
    bar: Candle,
    window: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
    monitor_from_ms: int,
) -> ConfirmationProgress:
    from app.services.ict_confirmation_engine import process_bar_sequential

    progress = process_bar_sequential(
        direction=direction,
        sequence=list(policy.sequence),
        confirmation_state=confirmation_state,
        bar=bar,
        window=window,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        invalidation_level=invalidation_level,
        structured=structured,
        monitor_from_ms=monitor_from_ms,
    )
    ts = dict(progress.confirmation_timestamps or {})
    ev = dict(progress.confirmation_evidence or {})
    for tok, tms in ts.items():
        if tok not in ev:
            ev[tok] = {"granularity": "5m", "engine": "STRICT", "bar_5m_ms": tms}
    progress.confirmation_evidence = ev
    return progress
