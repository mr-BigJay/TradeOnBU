"""Objective confirmation check for immediate LONG/SHORT at analysis persist time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_effective_settings
from app.domain.candle_utils import candle_close_ms
from app.domain.market_freshness import is_market_data_fresh
from app.domain.setup_lifecycle import ready_state_for_direction, wait_state_for_direction
from app.domain.setup_monitor_boundary import bars_closed_at_or_before_ms, immediate_replay_window_start_ms
from app.services.ict_confirmation_engine import ConfirmationProgress, all_required_met, process_bar_sequential
from app.services.market_data import KlineBundle, MarketDataService

MONITOR_INTERVAL = "5m"
MAX_REPLAY_BARS = 48


@dataclass
class ImmediateConfirmationEvaluation:
    ready: bool
    invalidated: bool
    confirmation_state: dict[str, Any]
    monitor_cursor_ms: int
    lifecycle_state: str
    last_validation_result: dict[str, Any]
    invalidation_reason: str | None = None


async def evaluate_immediate_objective_confirmations(
    *,
    symbol: str,
    direction: str,
    sequence: list[str],
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float,
    structured: dict[str, Any],
    persist_at: datetime,
    bundle: KlineBundle | None = None,
) -> ImmediateConfirmationEvaluation:
    """
    Replay only 5m bars **closed at or before** persist time (prospective — no future bars).
    Uses a bounded recent window for sweep/MSS/POI context and confirmation.
    """
    persist_at = persist_at if persist_at.tzinfo else persist_at.replace(tzinfo=timezone.utc)
    persist_ms = int(persist_at.timestamp() * 1000)
    settings = get_effective_settings()

    market = MarketDataService()
    if bundle is None:
        bundle = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, 120)

    conf_state: dict[str, Any] = {
        "sequence": list(sequence),
        "required": list(sequence),
        "completed": [],
        "missing": list(sequence),
        "confirmation_timestamps": {},
    }
    cursor = immediate_replay_window_start_ms(persist_ms, MONITOR_INTERVAL, MAX_REPLAY_BARS) - 1
    invalidated = False
    inv_reason: str | None = None

    if not bundle.data_complete or not bundle.closed:
        return ImmediateConfirmationEvaluation(
            ready=False,
            invalidated=False,
            confirmation_state=conf_state,
            monitor_cursor_ms=cursor,
            lifecycle_state=wait_state_for_direction(direction),
            last_validation_result={
                "ok": False,
                "reason": "missing_market_data_for_immediate_verify",
                "missing": list(sequence),
            },
        )

    closed_before_persist = bars_closed_at_or_before_ms(bundle.closed, persist_ms, MONITOR_INTERVAL)
    if not closed_before_persist:
        return ImmediateConfirmationEvaluation(
            ready=False,
            invalidated=False,
            confirmation_state=conf_state,
            monitor_cursor_ms=cursor,
            lifecycle_state=wait_state_for_direction(direction),
            last_validation_result={
                "ok": False,
                "reason": "no_closed_candles_at_persist",
                "missing": list(sequence),
            },
        )

    last_closed = closed_before_persist[-1]
    fresh, age_sec, allowed_age = is_market_data_fresh(
        last_closed, MONITOR_INTERVAL, persist_ms, settings.market_data_stale_seconds
    )
    if not fresh:
        return ImmediateConfirmationEvaluation(
            ready=False,
            invalidated=False,
            confirmation_state=conf_state,
            monitor_cursor_ms=cursor,
            lifecycle_state=wait_state_for_direction(direction),
            last_validation_result={
                "ok": False,
                "reason": "stale_market_data_at_persist",
                "age_since_close_sec": age_sec,
                "allowed_age_sec": allowed_age,
                "missing": list(sequence),
            },
        )

    window_start = immediate_replay_window_start_ms(persist_ms, MONITOR_INTERVAL, MAX_REPLAY_BARS)
    replay_bars = [b for b in closed_before_persist if b.time_ms >= window_start]
    replay_monitor_from = replay_bars[0].time_ms if replay_bars else window_start

    for bar in replay_bars:
        window = [c for c in closed_before_persist if c.time_ms <= bar.time_ms][-MAX_REPLAY_BARS:]
        progress = process_bar_sequential(
            direction=direction,
            sequence=sequence,
            confirmation_state=conf_state,
            bar=bar,
            window=window,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            invalidation_level=invalidation_level,
            structured=structured,
            monitor_from_ms=replay_monitor_from,
        )
        conf_state = {
            "sequence": list(sequence),
            "required": list(sequence),
            "completed": progress.completed,
            "missing": progress.missing,
            "confirmation_timestamps": progress.confirmation_timestamps,
        }
        cursor = bar.time_ms
        if progress.invalidated:
            invalidated = True
            inv_reason = progress.invalidation_reason
            break

    progress_check = ConfirmationProgress(
        sequence=sequence,
        completed=conf_state.get("completed") or [],
        missing=conf_state.get("missing") or [],
        confirmation_timestamps=conf_state.get("confirmation_timestamps") or {},
    )

    if invalidated:
        return ImmediateConfirmationEvaluation(
            ready=False,
            invalidated=True,
            confirmation_state=conf_state,
            monitor_cursor_ms=cursor,
            lifecycle_state=wait_state_for_direction(direction),
            last_validation_result={
                "ok": False,
                "reason": "structural_invalidation_at_persist",
                "detail": inv_reason,
            },
            invalidation_reason=inv_reason,
        )

    ready = all_required_met(progress_check)
    if ready:
        return ImmediateConfirmationEvaluation(
            ready=True,
            invalidated=False,
            confirmation_state=conf_state,
            monitor_cursor_ms=cursor,
            lifecycle_state=ready_state_for_direction(direction),
            last_validation_result={
                "ok": True,
                "source": "immediate_objective_verify_at_persist",
                "confirmed_on_closed_bars": True,
                "persist_ms": persist_ms,
                "last_closed_bar_ms": last_closed.time_ms,
            },
        )

    return ImmediateConfirmationEvaluation(
        ready=False,
        invalidated=False,
        confirmation_state=conf_state,
        monitor_cursor_ms=cursor,
        lifecycle_state=wait_state_for_direction(direction),
        last_validation_result={
            "ok": False,
            "reason": "missing_confirmations",
            "missing": conf_state.get("missing") or list(sequence),
            "source": "immediate_objective_verify_at_persist",
        },
    )
