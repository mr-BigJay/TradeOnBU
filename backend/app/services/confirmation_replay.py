"""Historical replay of ICT confirmations vs persisted monitor state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.opportunity_miss_reasons import (
    REPLAY_STATUS_INSUFFICIENT_HISTORY,
    REPLAY_STATUS_OK,
    REPLAY_STATUS_SEQUENCE_LIMITED,
)
from app.services.ict_confirmation_engine import (
    ConfirmationProgress,
    process_bar_sequential,
)
from app.services.monitor_kline_backfill import detect_skipped_closed_bars


@dataclass
class ReplayDiff:
    setup_id: str
    live_timestamps: dict[str, int]
    replay_timestamps: dict[str, int]
    missing_in_live: list[str]
    extra_in_live: list[str]
    order_mismatch: bool
    invalidated_replay: bool
    invalidated_live: bool
    replay_status: str = REPLAY_STATUS_OK
    replay_status_notes: list[str] = field(default_factory=list)
    single_token_per_5m_only: bool = True
    same_bar_multi_token_candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def assess_replay_history(
    *,
    closed_bars: list[Candle],
    monitor_from_ms: int,
    monitor_cursor_ms: int | None,
    now_ms: int,
    data_complete: bool,
    gaps_remaining: list[int],
    objective_notes: dict[str, Any] | None,
    confirmation_state: dict[str, Any] | None = None,
    interval: str = "5m",
) -> tuple[str, list[str], dict[str, Any] | None, int | None]:
    notes: list[str] = []
    if not closed_bars:
        return REPLAY_STATUS_INSUFFICIENT_HISTORY, ["no_closed_bars"], None, None

    in_range = [c for c in closed_bars if c.time_ms >= monitor_from_ms]
    if not in_range:
        return REPLAY_STATUS_INSUFFICIENT_HISTORY, ["no_bars_at_or_after_monitor_boundary"], None, None

    if in_range[0].time_ms > monitor_from_ms:
        notes.append(
            f"earliest_bar_open_ms={in_range[0].time_ms}_after_boundary={monitor_from_ms}"
        )
        return REPLAY_STATUS_INSUFFICIENT_HISTORY, notes, None, None

    if not data_complete or gaps_remaining:
        notes.append("market_bundle_incomplete_or_gaps")
        return REPLAY_STATUS_INSUFFICIENT_HISTORY, notes, None, None

    cursor = monitor_cursor_ms if monitor_cursor_ms is not None and monitor_cursor_ms >= 0 else monitor_from_ms - 1
    skipped = detect_skipped_closed_bars(
        closed_bars=closed_bars,
        cursor_ms=cursor,
        boundary_ms=monitor_from_ms,
        now_ms=now_ms,
        interval=interval,
    )
    if skipped:
        notes.append(f"skipped_closed_bars_in_bundle={skipped[:5]}")
        return REPLAY_STATUS_INSUFFICIENT_HISTORY, notes, None, None

    from app.services.revision_replay import assess_revision_replay_trust

    rev_status, rev_notes, seed, replay_from_ms = assess_revision_replay_trust(
        confirmation_state=confirmation_state or {},
        objective_notes=objective_notes,
        monitor_from_ms=monitor_from_ms,
    )
    if rev_status != REPLAY_STATUS_OK:
        return rev_status, notes + rev_notes, None, None

    return REPLAY_STATUS_OK, notes, seed, replay_from_ms


def replay_confirmations_on_5m_bars(
    *,
    direction: str,
    sequence: list[str],
    closed_bars: list[Candle],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float | None,
    structured: dict[str, Any],
    monitor_from_ms: int,
    initial_confirmation_state: dict[str, Any] | None = None,
    replay_from_ms: int | None = None,
) -> ConfirmationProgress:
    """Full replay from scenario boundary (independent of monitor cursor).

    When ``initial_confirmation_state`` is set (post-revision merge snapshot), replay continues
    from validated confirmations instead of re-deriving the full prefix from candles.
    """
    if initial_confirmation_state:
        state = {
            "confirmation_timestamps": dict(initial_confirmation_state.get("confirmation_timestamps") or {}),
            "confirmation_evidence": dict(initial_confirmation_state.get("confirmation_evidence") or {}),
        }
    else:
        state = {"confirmation_timestamps": {}, "confirmation_evidence": {}}
    effective_from = replay_from_ms if replay_from_ms is not None else monitor_from_ms
    cursor = effective_from - 1
    last: ConfirmationProgress | None = None
    sorted_bars = sorted(closed_bars, key=lambda c: c.time_ms)
    for bar in sorted_bars:
        if bar.time_ms <= cursor:
            continue
        if bar.time_ms < effective_from:
            continue
        window = [c for c in sorted_bars if c.time_ms <= bar.time_ms][-48:]
        last = process_bar_sequential(
            direction=direction,
            sequence=sequence,
            confirmation_state=state,
            bar=bar,
            window=window,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            invalidation_level=invalidation_level,
            structured=structured,
            monitor_from_ms=monitor_from_ms,
        )
        state = {
            "confirmation_timestamps": last.confirmation_timestamps,
            "confirmation_evidence": last.confirmation_evidence,
        }
        if last.invalidated:
            break
    if last is not None:
        return last
    timestamps = dict(state.get("confirmation_timestamps") or {})
    evidence = dict(state.get("confirmation_evidence") or {})
    completed = [t for t in sequence if t in timestamps]
    missing = [t for t in sequence if t not in timestamps]
    last_bar = max(timestamps.values()) if timestamps else max(effective_from - 1, monitor_from_ms - 1)
    return ConfirmationProgress(
        sequence=list(sequence),
        completed=completed,
        missing=missing,
        confirmation_timestamps=timestamps,
        confirmation_evidence=evidence,
        last_bar_ms=int(last_bar),
    )


def _tokens_true_on_bar(
    *,
    direction: str,
    sequence: list[str],
    bar: Candle,
    window: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
) -> list[str]:
    from app.services.ict_confirmation_engine import _token_on_bar

    hits: list[str] = []
    for tok in sequence:
        if _token_on_bar(
            tok,
            direction=direction,
            window=window,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
        ):
            hits.append(tok)
    return hits


_ICT_MIN_WINDOW_BARS = 13  # liquidity_sweep / mss lookback (12) + evaluating bar


def _synthesize_partial_5m_from_1m(partial_1m: list[Candle]) -> Candle:
    first = partial_1m[0]
    return Candle(
        time_ms=first.time_ms,
        open=first.open,
        high=max(c.high for c in partial_1m),
        low=min(c.low for c in partial_1m),
        close=partial_1m[-1].close,
        volume=sum((c.volume or 0.0) for c in partial_1m),
    )


def _ict_window_at_1m_close(
    *,
    closed_5m: list[Candle],
    bar_5m: Candle,
    closed_1m: list[Candle],
    eval_1m_ms: int,
) -> list[Candle]:
    """5m ICT window available at a closed 1m instant (no future 1m/5m)."""
    prior_5m = sorted([b for b in closed_5m if b.time_ms < bar_5m.time_ms], key=lambda c: c.time_ms)[-47:]
    partial_1m = sorted(
        [c for c in closed_1m if bar_5m.time_ms <= c.time_ms <= eval_1m_ms],
        key=lambda c: c.time_ms,
    )
    if not partial_1m:
        return prior_5m[-48:]
    synth = _synthesize_partial_5m_from_1m(partial_1m)
    return (prior_5m + [synth])[-48:]


def _first_token_hit_ms_on_1m(
    *,
    direction: str,
    token: str,
    closed_5m: list[Candle],
    bar_5m: Candle,
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
    now_ms: int,
) -> int | None:
    from app.services.ict_confirmation_engine import _token_on_bar

    close_5m = candle_close_ms(bar_5m, "5m")
    for c in sorted(
        [x for x in closed_1m if bar_5m.time_ms <= x.time_ms < close_5m],
        key=lambda x: x.time_ms,
    ):
        if now_ms < candle_close_ms(c, "1m"):
            continue
        window = _ict_window_at_1m_close(
            closed_5m=closed_5m,
            bar_5m=bar_5m,
            closed_1m=closed_1m,
            eval_1m_ms=c.time_ms,
        )
        if token in {"trend_pullback", "trend_resume", "breakout_close", "breakout_retest"}:
            window_for_tok = sorted([b for b in closed_1m if b.time_ms <= c.time_ms], key=lambda b: b.time_ms)[-48:]
            bar = c
        elif token in ("entry_zone_touch", "order_block_retest"):
            window_for_tok = [c]
            bar = c
        else:
            if len(window) < _ICT_MIN_WINDOW_BARS:
                continue
            window_for_tok = window
            bar = window[-1]
        if _token_on_bar(
            token,
            direction=direction,
            window=window_for_tok,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
        ):
            return c.time_ms
    return None


def chronology_provable_on_1m(
    *,
    direction: str,
    sequence: list[str],
    hits_on_5m: list[str],
    bar_5m: Candle,
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
    now_ms: int,
) -> tuple[bool, str]:
    """
    True only when all five 1m slots in the 5m window are present & closed,
    required tokens fire in ICT sequence order on strictly increasing 1m opens,
    without using future 1m data (look-ahead safe).
    """
    step1 = interval_ms("1m")
    close_ms = candle_close_ms(bar_5m, "5m")
    expected_opens = [bar_5m.time_ms + i * step1 for i in range(5)]
    slot_map = {c.time_ms: c for c in closed_1m if bar_5m.time_ms <= c.time_ms < close_ms}
    for open_ms in expected_opens:
        c = slot_map.get(open_ms)
        if c is None:
            return False, "incomplete_1m_coverage"
        if now_ms < candle_close_ms(c, "1m"):
            return False, "1m_bar_not_closed_at_eval_time"

    window_5m = [c for c in closed_5m if c.time_ms <= bar_5m.time_ms][-48:]
    hits_at_5m_close = _tokens_true_on_bar(
        direction=direction,
        sequence=sequence,
        bar=bar_5m,
        window=window_5m,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        structured=structured,
    )
    if sorted(hits_on_5m) != sorted(hits_at_5m_close):
        return False, "5m_hits_inconsistent_with_close_window"

    ordered_hits = [t for t in sequence if t in hits_on_5m]
    if len(ordered_hits) < 2:
        return False, "single_token_on_5m"

    first_ms: dict[str, int] = {}
    for tok in ordered_hits:
        ms = _first_token_hit_ms_on_1m(
            direction=direction,
            token=tok,
            closed_5m=closed_5m,
            bar_5m=bar_5m,
            closed_1m=closed_1m,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
            now_ms=now_ms,
        )
        if ms is None:
            return False, f"token_{tok}_not_observed_on_1m"
        first_ms[tok] = ms

    for i in range(len(ordered_hits) - 1):
        t1, t2 = ordered_hits[i], ordered_hits[i + 1]
        if first_ms[t1] >= first_ms[t2]:
            return False, f"order_violation_{t1}_not_before_{t2}"

    return True, "ok"


def detect_same_5m_multi_token_candidates(
    *,
    direction: str,
    sequence: list[str],
    closed_5m: list[Candle],
    closed_1m: list[Candle],
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any],
    monitor_from_ms: int,
    now_ms: int,
) -> list[dict[str, Any]]:
    """
    Flag 5m bars where multiple sequence tokens would fire on the same bar (engine records only one).
    """
    out: list[dict[str, Any]] = []
    for bar in closed_5m:
        if bar.time_ms < monitor_from_ms:
            continue
        window = [c for c in closed_5m if c.time_ms <= bar.time_ms][-48:]
        hits = _tokens_true_on_bar(
            direction=direction,
            sequence=sequence,
            bar=bar,
            window=window,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
        )
        if len(hits) < 2:
            continue
        provable, reason = chronology_provable_on_1m(
            direction=direction,
            sequence=sequence,
            hits_on_5m=hits,
            bar_5m=bar,
            closed_5m=closed_5m,
            closed_1m=closed_1m,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=structured,
            now_ms=now_ms,
        )
        out.append(
            {
                "bar_open_ms": bar.time_ms,
                "tokens_on_5m_close_window": hits,
                "chronology_provable_on_1m": provable,
                "chronology_reason": reason,
            }
        )
    return out


def diff_live_vs_replay(
    *,
    setup_id: str,
    live_state: dict[str, Any],
    replay: ConfirmationProgress,
    multi_candidates: list[dict[str, Any]] | None = None,
    replay_status: str = REPLAY_STATUS_OK,
    replay_status_notes: list[str] | None = None,
) -> ReplayDiff:
    live_ts = dict(live_state.get("confirmation_timestamps") or {})
    replay_ts = dict(replay.confirmation_timestamps or {})
    live_keys = set(live_ts)
    replay_keys = set(replay_ts)
    order_mismatch = False
    for k in live_keys | replay_keys:
        if live_ts.get(k) != replay_ts.get(k):
            order_mismatch = True
            break
    notes: list[str] = list(replay_status_notes or [])
    if multi_candidates:
        unproven = [
            m
            for m in multi_candidates
            if len(m.get("tokens_on_5m_close_window") or []) > 1 and not m.get("chronology_provable_on_1m")
        ]
        if unproven:
            notes.append(f"{len(unproven)} five_m bars had multi-token signals without provable 1m order")
    missing_in_live: list[str] = []
    extra_in_live: list[str] = []
    if replay_status == REPLAY_STATUS_OK:
        missing_in_live = sorted(replay_keys - live_keys)
        extra_in_live = sorted(live_keys - replay_keys)
    return ReplayDiff(
        setup_id=setup_id,
        live_timestamps=live_ts,
        replay_timestamps=replay_ts,
        missing_in_live=missing_in_live,
        extra_in_live=extra_in_live,
        order_mismatch=order_mismatch if replay_status == REPLAY_STATUS_OK else False,
        invalidated_replay=bool(replay.invalidated),
        invalidated_live=(live_state.get("invalidated") is True),
        replay_status=replay_status,
        replay_status_notes=list(replay_status_notes or []),
        same_bar_multi_token_candidates=multi_candidates or [],
        notes=notes,
    )
