"""Objective confirmation check for immediate LONG/SHORT at analysis persist time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.candle_utils import candle_close_ms
from app.domain.setup_lifecycle import ready_state_for_direction, wait_state_for_direction
from app.services.ict_confirmation_engine import all_required_met, process_bar_sequential
from app.services.market_data import KlineBundle, MarketDataService

MONITOR_INTERVAL = "5m"


@dataclass
class ImmediateConfirmationEvaluation:
    ready: bool
    invalidated: bool
    confirmation_state: dict[str, Any]
    monitor_cursor_ms: int
    lifecycle_state: str
    last_validation_result: dict[str, Any]
    invalidation_reason: str | None = None


def _eligible_bars_up_to_now(closed: list, boundary_ms: int, now_ms: int) -> list:
    out = []
    for bar in closed:
        if bar.time_ms < boundary_ms:
            continue
        if now_ms < candle_close_ms(bar, MONITOR_INTERVAL):
            continue
        out.append(bar)
    return out


async def evaluate_immediate_objective_confirmations(
    *,
    symbol: str,
    direction: str,
    sequence: list[str],
    boundary_ms: int,
    zone_lo: float,
    zone_hi: float,
    invalidation_level: float,
    structured: dict[str, Any],
    bundle: KlineBundle | None = None,
    now_ms: int | None = None,
) -> ImmediateConfirmationEvaluation:
    """
    Replay closed 5m bars available at analysis time (no look-ahead).
    Does not create orders or backdate fills — only determines WAIT vs READY lifecycle.
    """
    market = MarketDataService()
    if bundle is None:
        bundle = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, 120)
    if now_ms is None:
        if bundle.closed:
            last = bundle.closed[-1]
            now_ms = candle_close_ms(last, MONITOR_INTERVAL) + 5_000
        else:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    conf_state: dict[str, Any] = {
        "sequence": list(sequence),
        "required": list(sequence),
        "completed": [],
        "missing": list(sequence),
        "confirmation_timestamps": {},
    }
    cursor = boundary_ms - 1
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

    bars = _eligible_bars_up_to_now(bundle.closed, boundary_ms, now_ms)
    for bar in bars:
        window = [c for c in bundle.closed if c.time_ms <= bar.time_ms][-48:]
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
            monitor_from_ms=boundary_ms,
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

    from app.services.ict_confirmation_engine import ConfirmationProgress

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
            },
        )

    return ImmediateConfirmationEvaluation(
        ready=False,
        invalidated=False,
        confirmation_state=conf_state,
        monitor_cursor_ms=cursor,
        lifecycle_state=f"WAIT_{direction.upper()}",
        last_validation_result={
            "ok": False,
            "reason": "missing_confirmations",
            "missing": conf_state.get("missing") or list(sequence),
            "source": "immediate_objective_verify_at_persist",
        },
    )
