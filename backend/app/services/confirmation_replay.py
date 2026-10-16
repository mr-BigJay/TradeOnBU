"""Historical replay of ICT confirmations vs persisted monitor state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms, interval_ms
from app.services.ict_confirmation_engine import (
    ConfirmationProgress,
    process_bar_sequential,
)


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
    single_token_per_5m_only: bool = True
    same_bar_multi_token_candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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
    start_cursor_ms: int | None = None,
) -> ConfirmationProgress:
    state: dict[str, Any] = {"confirmation_timestamps": {}, "confirmation_evidence": {}}
    cursor = (start_cursor_ms if start_cursor_ms is not None else monitor_from_ms - 1)
    last: ConfirmationProgress | None = None
    for bar in sorted(closed_bars, key=lambda c: c.time_ms):
        if bar.time_ms <= cursor:
            continue
        window = [c for c in closed_bars if c.time_ms <= bar.time_ms][-48:]
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
    return last or ConfirmationProgress(sequence=list(sequence))


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
) -> list[dict[str, Any]]:
    """
    Flag 5m bars where multiple sequence tokens would fire on the same bar (engine records only one).
    Uses 1m bars inside the 5m window to see if temporal order could be proven.
    """
    out: list[dict[str, Any]] = []
    step5 = interval_ms("5m")
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
        close_ms = candle_close_ms(bar, "5m")
        intrabar = [c for c in closed_1m if bar.time_ms <= c.time_ms < close_ms]
        intrabar_order: list[dict[str, Any]] = []
        for c in intrabar:
            w1 = [x for x in intrabar if x.time_ms <= c.time_ms][-20:]
            sub_hits = _tokens_true_on_bar(
                direction=direction,
                sequence=sequence,
                bar=c,
                window=w1,
                zone_lo=zone_lo,
                zone_hi=zone_hi,
                structured=structured,
            )
            if sub_hits:
                intrabar_order.append({"open_ms": c.time_ms, "tokens": sub_hits})
        provable = len(intrabar_order) >= 2 and all(
            intrabar_order[i]["open_ms"] <= intrabar_order[i + 1]["open_ms"] for i in range(len(intrabar_order) - 1)
        )
        out.append(
            {
                "bar_open_ms": bar.time_ms,
                "tokens_on_5m_close_window": hits,
                "intrabar_1m_hits": intrabar_order,
                "chronology_provable_on_1m": provable,
            }
        )
    return out


def diff_live_vs_replay(
    *,
    setup_id: str,
    live_state: dict[str, Any],
    replay: ConfirmationProgress,
    multi_candidates: list[dict[str, Any]] | None = None,
) -> ReplayDiff:
    live_ts = dict(live_state.get("confirmation_timestamps") or {})
    replay_ts = dict(replay.confirmation_timestamps or {})
    live_keys = set(live_ts)
    replay_keys = set(replay_ts)
    order_mismatch = False
    if live_ts and replay_ts:
        for k in live_keys | replay_keys:
            if live_ts.get(k) != replay_ts.get(k):
                order_mismatch = True
                break
    notes: list[str] = []
    if multi_candidates:
        unproven = [m for m in multi_candidates if len(m.get("tokens_on_5m_close_window") or []) > 1 and not m.get("chronology_provable_on_1m")]
        if unproven:
            notes.append(f"{len(unproven)} five_m bars had multi-token signals without 1m chronology proof")
    return ReplayDiff(
        setup_id=setup_id,
        live_timestamps=live_ts,
        replay_timestamps=replay_ts,
        missing_in_live=sorted(replay_keys - live_keys),
        extra_in_live=sorted(live_keys - replay_keys),
        order_mismatch=order_mismatch,
        invalidated_replay=bool(replay.invalidated),
        invalidated_live=(live_state.get("invalidated") is True),
        same_bar_multi_token_candidates=multi_candidates or [],
        notes=notes,
    )
