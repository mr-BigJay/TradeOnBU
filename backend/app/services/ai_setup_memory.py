from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.candles import Candle
from app.models import AiAnalysisRun, AiProposedSetup
from app.services.market_data import KlineBundle, MarketDataService

LIFECYCLE_ENTRY_NOT_REACHED = "entry_not_reached"
LIFECYCLE_ENTRY_TOUCHED = "entry_touched"
LIFECYCLE_SL_BEFORE_ENTRY = "sl_before_entry"
LIFECYCLE_TP_BEFORE_ENTRY = "tp_before_entry"
LIFECYCLE_SL_AFTER_ENTRY = "sl_after_entry"
LIFECYCLE_TP_AFTER_ENTRY = "tp_after_entry"
LIFECYCLE_SAME_CANDLE_AMBIGUOUS = "same_candle_ambiguous"
LIFECYCLE_INVALIDATED = "invalidated"
LIFECYCLE_EXPIRED = "expired"
LIFECYCLE_UNKNOWN_MISSING_DATA = "unknown_missing_data"

TRACKING_COMPLETE = "complete"
TRACKING_INCOMPLETE = "incomplete"

VALIDITY_ACTIVE = "active"
VALIDITY_EXPIRED = "expired"

MAX_EXPIRES_HOURS = 168.0
MIN_EXPIRES_HOURS = 1.0
LEVEL_DEDUPE_TOLERANCE_PCT = 0.25


def _utc(dt: datetime | None = None) -> datetime:
    return dt or datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _notes(setup: AiProposedSetup) -> dict[str, Any]:
    return dict(setup.objective_notes or {})


def _save_notes(setup: AiProposedSetup, notes: dict[str, Any]) -> None:
    setup.objective_notes = notes


POST_ENTRY_TERMINAL_PHASES = (
    LIFECYCLE_SL_AFTER_ENTRY,
    LIFECYCLE_TP_AFTER_ENTRY,
    LIFECYCLE_SAME_CANDLE_AMBIGUOUS,
    LIFECYCLE_INVALIDATED,
)


def _has_blocking_tracking_issues(notes: dict[str, Any]) -> bool:
    if notes.get("fetch_error"):
        return True
    if notes.get("pending_tracking_from_ms") is not None:
        return True
    if notes.get("missing_intervals"):
        return True
    gaps = (notes.get("data_gaps") or {}).get("gaps_remaining")
    return bool(gaps)


def _tracking_start_ms(setup: AiProposedSetup) -> int:
    created_ms = int(_ensure_utc(setup.created_at).timestamp() * 1000)
    step = interval_ms("5m")
    return ((created_ms // step) + 1) * step


def _last_required_tracking_ms(setup: AiProposedSetup, interval: str = "5m") -> int:
    start_ms = _tracking_start_ms(setup)
    if not setup.expires_at:
        return start_ms
    exp_ms = int(_ensure_utc(setup.expires_at).timestamp() * 1000)
    step = interval_ms(interval)
    last_open = (exp_ms // step) * step
    while last_open + step > exp_ms:
        last_open -= step
    return max(start_ms, last_open)


def _candle_straddles_expiry(setup: AiProposedSetup, candle: Candle, interval: str = "5m") -> bool:
    if not setup.expires_at:
        return False
    exp_ms = int(_ensure_utc(setup.expires_at).timestamp() * 1000)
    close_ms = candle_close_ms(candle, interval)
    return candle.time_ms < exp_ms < close_ms


def _candle_fully_eligible_before_expiry(setup: AiProposedSetup, candle: Candle, interval: str = "5m") -> bool:
    """Bar must fully close at or before expiry to prove pre-expiry touches."""
    if not setup.expires_at:
        return True
    exp_ms = int(_ensure_utc(setup.expires_at).timestamp() * 1000)
    return candle_close_ms(candle, interval) <= exp_ms


def _validity_window_needs_more_tracking(setup: AiProposedSetup, now: datetime) -> bool:
    wm = setup.last_tracked_candle_ms or 0
    if wm <= 0:
        return True
    last_req = _last_required_tracking_ms(setup)
    if not setup.expires_at:
        return False
    if _ensure_utc(setup.expires_at) > now:
        now_ms = int(now.timestamp() * 1000)
        step = interval_ms("5m")
        current_target = max(_tracking_start_ms(setup), (now_ms // step) * step - step)
        target = min(last_req, current_target)
        return wm < target
    return wm < last_req


def _tracking_incomplete(setup: AiProposedSetup, now: datetime | None = None) -> bool:
    notes = setup.objective_notes or {}
    phase = notes.get("lifecycle_phase")
    if phase == LIFECYCLE_INVALIDATED and setup.status == "invalidated":
        return False
    if _has_blocking_tracking_issues(notes):
        return True
    return _validity_window_needs_more_tracking(setup, now or _utc())


def _clear_resolved_tracking_errors(notes: dict[str, Any], bundle: KlineBundle | None) -> None:
    if bundle is None:
        return
    if bundle.gaps_remaining or not bundle.data_complete:
        return
    for key in (
        "fetch_error",
        "pending_tracking_from_ms",
        "missing_intervals",
        "data_gaps",
        "tracking_warning",
    ):
        notes.pop(key, None)


def _is_post_entry_terminal_phase(phase: str | None) -> bool:
    return phase in POST_ENTRY_TERMINAL_PHASES


def _candle_within_validity(setup: AiProposedSetup, candle: Candle, interval: str = "5m") -> bool:
    """Fully closed before expiry (open straddling expiry is handled separately)."""
    if _candle_straddles_expiry(setup, candle, interval):
        return False
    return _candle_fully_eligible_before_expiry(setup, candle, interval)


def _partition_candles_for_validity(
    setup: AiProposedSetup,
    candles: list[Candle],
    *,
    watermark_ms: int,
    interval: str = "5m",
) -> tuple[list[Candle], list[int]]:
    eligible: list[Candle] = []
    straddle_ms: list[int] = []
    for c in sorted(candles, key=lambda x: x.time_ms):
        if c.time_ms <= watermark_ms:
            continue
        if _candle_straddles_expiry(setup, c, interval):
            straddle_ms.append(c.time_ms)
            continue
        if _candle_fully_eligible_before_expiry(setup, c, interval):
            eligible.append(c)
    return eligible, straddle_ms


def _verified_contiguous_prefix(scoped: list[Candle], bundle: KlineBundle | None) -> list[Candle]:
    """Only candles in the verified contiguous prefix before the first unresolved gap."""
    if not scoped:
        return []
    ordered = sorted(scoped, key=lambda c: c.time_ms)
    if bundle is None:
        return ordered
    if bundle.gaps_remaining:
        first_gap = min(bundle.gaps_remaining)
        return [c for c in ordered if c.time_ms < first_gap]
    if not bundle.data_complete:
        step = interval_ms(bundle.interval or "5m")
        prefix: list[Candle] = [ordered[0]]
        for c in ordered[1:]:
            if c.time_ms - prefix[-1].time_ms > int(step * 1.5):
                break
            prefix.append(c)
        return prefix
    return ordered


def _append_revision(
    setup: AiProposedSetup,
    *,
    source_run_id: int,
    structured: dict[str, Any],
    proposed: dict[str, Any],
    kind: str,
) -> None:
    notes = _notes(setup)
    history = notes.setdefault("revision_history", [])
    history.append(
        {
            "revised_at": _utc().isoformat(),
            "source_run_id": source_run_id,
            "kind": kind,
            "thesis_fa": proposed.get("thesis_fa") or structured.get("thesis_fa"),
            "proposed_levels": {
                "entry_price": proposed.get("entry_price") or structured.get("entry_price"),
                "stop_loss": proposed.get("stop_loss") or structured.get("stop_loss"),
                "take_profit": proposed.get("take_profit") or structured.get("take_profit"),
            },
            "report_excerpt_fa": (structured.get("report_fa") or "")[:600],
        }
    )
    _save_notes(setup, notes)


def _store_original_forecast(setup: AiProposedSetup) -> None:
    notes = _notes(setup)
    if notes.get("original_forecast"):
        return
    notes["original_forecast"] = {
        "run_id": setup.run_id,
        "thesis_fa": setup.thesis_fa,
        "entry_price": setup.entry_price,
        "entry_zone_low": setup.entry_zone_low,
        "entry_zone_high": setup.entry_zone_high,
        "stop_loss": setup.stop_loss,
        "take_profit": setup.take_profit,
        "invalidation": setup.invalidation,
        "invalidation_level": setup.invalidation_level,
        "created_at": setup.created_at.isoformat() if setup.created_at else None,
    }
    notes.setdefault("validity_status", VALIDITY_ACTIVE)
    notes.setdefault("outcome_tracking_status", TRACKING_INCOMPLETE)
    _save_notes(setup, notes)


def serialize_setup_row(row: AiProposedSetup) -> dict[str, Any]:
    notes = row.objective_notes or {}
    return {
        "setup_id": row.setup_id,
        "run_id": row.run_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "status": row.status,
        "validity_status": notes.get("validity_status", VALIDITY_ACTIVE),
        "outcome_tracking_status": notes.get("outcome_tracking_status", TRACKING_INCOMPLETE),
        "lifecycle_phase": notes.get("lifecycle_phase", LIFECYCLE_ENTRY_NOT_REACHED),
        "thesis_fa": row.thesis_fa,
        "original_forecast": notes.get("original_forecast"),
        "revision_history": notes.get("revision_history") or [],
        "entry_price": row.entry_price,
        "entry_zone_low": row.entry_zone_low,
        "entry_zone_high": row.entry_zone_high,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "invalidation": row.invalidation,
        "invalidation_level": row.invalidation_level,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "entry_touched": row.entry_touched,
        "sl_touched_post_entry": row.sl_touched,
        "tp_touched_post_entry": row.tp_touched,
        "sequencing_ambiguous": row.sequencing_ambiguous,
        "paper_fill_recorded": row.paper_fill_recorded,
        "first_touch_ms": notes.get("first_touch_ms") or {},
        "objective_notes": notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }


async def load_trackable_setups(session: AsyncSession, symbol: str) -> list[AiProposedSetup]:
    """Setups still subject to objective price tracking (includes expired-but-reconciling)."""
    rows = (
        await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status.in_(("open", "expired")))
            .order_by(desc(AiProposedSetup.created_at))
            .limit(40)
        )
    ).scalars().all()
    out: list[AiProposedSetup] = []
    for row in rows:
        if row.status == "open":
            out.append(row)
        elif row.status == "expired" and _tracking_incomplete(row):
            out.append(row)
    return out


async def load_open_setups(session: AsyncSession, symbol: str) -> list[AiProposedSetup]:
    rows = (
        await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status == "open")
            .order_by(desc(AiProposedSetup.created_at))
            .limit(20)
        )
    ).scalars().all()
    return list(rows)


def _finalize_validity_and_tracking(setup: AiProposedSetup, now: datetime | None = None) -> None:
    now = now or _utc()
    notes = _notes(setup)
    if setup.expires_at and _ensure_utc(setup.expires_at) <= now:
        notes["validity_status"] = VALIDITY_EXPIRED
    incomplete = _tracking_incomplete(setup, now)
    if incomplete:
        notes["outcome_tracking_status"] = TRACKING_INCOMPLETE
    else:
        notes["outcome_tracking_status"] = TRACKING_COMPLETE
        if notes.get("validity_status") == VALIDITY_EXPIRED and setup.status == "open":
            setup.status = "expired"
            setup.closed_at = now
            phase = notes.get("lifecycle_phase")
            if phase not in POST_ENTRY_TERMINAL_PHASES:
                notes["lifecycle_phase"] = LIFECYCLE_EXPIRED
    _save_notes(setup, notes)


async def load_recent_closed_setups(session: AsyncSession, symbol: str, limit: int = 12) -> list[AiProposedSetup]:
    rows = (
        await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status != "open")
            .order_by(desc(AiProposedSetup.closed_at), desc(AiProposedSetup.id))
            .limit(limit)
        )
    ).scalars().all()
    return [r for r in rows if not _tracking_incomplete(r)]


async def load_recent_analysis_summaries(session: AsyncSession, limit: int = 5) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(AiAnalysisRun)
            .where(AiAnalysisRun.is_ai.is_(True))
            .where(AiAnalysisRun.status.like("completed%"))
            .order_by(desc(AiAnalysisRun.id))
            .limit(limit)
        )
    ).scalars().all()
    out = []
    for r in rows:
        meta = ((r.structured or {}).get("_artifacts") or {}).get("inference") or {}
        out.append(
            {
                "run_id": r.id,
                "decision": r.decision,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "report_preview_fa": (r.report_fa or "")[:400],
                "model": meta.get("requested_model") or r.model_slug,
                "reasoning_effort": meta.get("requested_reasoning_effort"),
            }
        )
    return out


def _zone_bounds(row: AiProposedSetup) -> tuple[float | None, float | None]:
    lo = row.entry_zone_low
    hi = row.entry_zone_high
    if lo is not None and hi is not None:
        return min(lo, hi), max(lo, hi)
    if row.entry_price is not None:
        pad = row.entry_price * 0.001
        return row.entry_price - pad, row.entry_price + pad
    return None, None


def _touch_entry(c: Candle, entry_lo: float | None, entry_hi: float | None) -> bool:
    if entry_lo is None or entry_hi is None:
        return False
    return c.low <= entry_hi and c.high >= entry_lo


def _touch_sl(c: Candle, direction: str, sl: float | None) -> bool:
    if sl is None:
        return False
    if direction == "LONG":
        return c.low <= sl
    if direction == "SHORT":
        return c.high >= sl
    return False


def _touch_tp(c: Candle, direction: str, tp: float | None) -> bool:
    if tp is None:
        return False
    if direction == "LONG":
        return c.high >= tp
    if direction == "SHORT":
        return c.low <= tp
    return False


def _record_first_touch(notes: dict[str, Any], key: str, time_ms: int) -> None:
    ft = notes.setdefault("first_touch_ms", {})
    if key not in ft:
        ft[key] = time_ms


def apply_objective_price_tracking(
    setup: AiProposedSetup,
    candles: list[Candle],
    *,
    bundle: KlineBundle | None = None,
    watermark_ms: int = 0,
) -> None:
    """Chronological objective touches — never implies a paper fill."""
    notes = _notes(setup)
    phase = notes.get("lifecycle_phase", LIFECYCLE_ENTRY_NOT_REACHED)
    if phase == LIFECYCLE_SAME_CANDLE_AMBIGUOUS and not candles:
        return

    if not candles:
        if bundle and (bundle.gaps_remaining or not bundle.data_complete):
            notes["tracking_warning"] = "No verified candles in window; history may be incomplete."
            notes["outcome_tracking_status"] = TRACKING_INCOMPLETE
            phase_now = notes.get("lifecycle_phase") or LIFECYCLE_ENTRY_NOT_REACHED
            if phase_now == LIFECYCLE_ENTRY_NOT_REACHED:
                notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
            _save_notes(setup, notes)
        return

    direction = (setup.direction or "").upper()
    entry_lo, entry_hi = _zone_bounds(setup)
    sl = setup.stop_loss
    tp = setup.take_profit
    inv = setup.invalidation_level
    notes.setdefault("disclaimer", "Touches are objective price observations, not paper fills.")
    phase = notes.get("lifecycle_phase", LIFECYCLE_ENTRY_NOT_REACHED)
    entry_reached = setup.entry_touched or phase in (
        LIFECYCLE_ENTRY_TOUCHED,
        LIFECYCLE_SL_AFTER_ENTRY,
        LIFECYCLE_TP_AFTER_ENTRY,
        LIFECYCLE_SAME_CANDLE_AMBIGUOUS,
    )

    sorted_candles, straddle_ms = _partition_candles_for_validity(
        setup, candles, watermark_ms=watermark_ms, interval="5m"
    )
    if straddle_ms:
        notes["expiry_boundary_uncertain"] = True
        notes["expiry_boundary_straddle_candles_ms"] = straddle_ms[:20]

    for c in sorted_candles:
        if phase == LIFECYCLE_SAME_CANDLE_AMBIGUOUS:
            break
        if _is_post_entry_terminal_phase(phase):
            continue

        entry_hit = _touch_entry(c, entry_lo, entry_hi)
        sl_hit = _touch_sl(c, direction, sl)
        tp_hit = _touch_tp(c, direction, tp)

        if not entry_reached:
            if entry_hit and (sl_hit or tp_hit):
                setup.sequencing_ambiguous = True
                setup.entry_touched = True
                entry_reached = True
                notes["lifecycle_phase"] = LIFECYCLE_SAME_CANDLE_AMBIGUOUS
                phase = LIFECYCLE_SAME_CANDLE_AMBIGUOUS
                _record_first_touch(notes, "entry", c.time_ms)
                if sl_hit:
                    _record_first_touch(notes, "sl_same_candle", c.time_ms)
                if tp_hit:
                    _record_first_touch(notes, "tp_same_candle", c.time_ms)
                continue

            if sl_hit:
                _record_first_touch(notes, "sl_pre_entry", c.time_ms)
                notes["lifecycle_phase"] = LIFECYCLE_SL_BEFORE_ENTRY
                notes["sl_pre_entry"] = True
            if tp_hit:
                _record_first_touch(notes, "tp_pre_entry", c.time_ms)
                if notes.get("lifecycle_phase") != LIFECYCLE_SL_BEFORE_ENTRY:
                    notes["lifecycle_phase"] = LIFECYCLE_TP_BEFORE_ENTRY
                notes["tp_pre_entry"] = True

            if entry_hit:
                setup.entry_touched = True
                entry_reached = True
                _record_first_touch(notes, "entry", c.time_ms)
                notes["lifecycle_phase"] = LIFECYCLE_ENTRY_TOUCHED
                phase = LIFECYCLE_ENTRY_TOUCHED
            elif phase == LIFECYCLE_ENTRY_NOT_REACHED and not sl_hit and not tp_hit:
                notes["lifecycle_phase"] = LIFECYCLE_ENTRY_NOT_REACHED

            if inv is not None and not entry_reached:
                if direction == "LONG" and c.close < inv:
                    setup.status = "invalidated"
                    setup.closed_at = _utc()
                    notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                    notes["outcome_tracking_status"] = TRACKING_COMPLETE
                    _record_first_touch(notes, "invalidation", c.time_ms)
                    _save_notes(setup, notes)
                    return
                if direction == "SHORT" and c.close > inv:
                    setup.status = "invalidated"
                    setup.closed_at = _utc()
                    notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                    notes["outcome_tracking_status"] = TRACKING_COMPLETE
                    _record_first_touch(notes, "invalidation", c.time_ms)
                    _save_notes(setup, notes)
                    return
            continue

        if inv is not None:
            if direction == "LONG" and c.close < inv:
                setup.status = "invalidated"
                setup.closed_at = _utc()
                notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                notes["outcome_tracking_status"] = TRACKING_COMPLETE
                _record_first_touch(notes, "invalidation", c.time_ms)
                _save_notes(setup, notes)
                return
            if direction == "SHORT" and c.close > inv:
                setup.status = "invalidated"
                setup.closed_at = _utc()
                notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                notes["outcome_tracking_status"] = TRACKING_COMPLETE
                _record_first_touch(notes, "invalidation", c.time_ms)
                _save_notes(setup, notes)
                return

        if sl_hit and tp_hit:
            setup.sequencing_ambiguous = True
            _record_first_touch(notes, "sl_post_entry", c.time_ms)
            _record_first_touch(notes, "tp_post_entry", c.time_ms)
            notes["lifecycle_phase"] = LIFECYCLE_SAME_CANDLE_AMBIGUOUS
            notes["post_entry_same_candle"] = True
            phase = LIFECYCLE_SAME_CANDLE_AMBIGUOUS
            continue
        if sl_hit:
            _record_first_touch(notes, "sl_post_entry", c.time_ms)
            setup.sl_touched = True
            notes["lifecycle_phase"] = LIFECYCLE_SL_AFTER_ENTRY
            phase = LIFECYCLE_SL_AFTER_ENTRY
        elif tp_hit:
            _record_first_touch(notes, "tp_post_entry", c.time_ms)
            setup.tp_touched = True
            notes["lifecycle_phase"] = LIFECYCLE_TP_AFTER_ENTRY
            phase = LIFECYCLE_TP_AFTER_ENTRY

    if bundle and (bundle.gaps_remaining or not bundle.data_complete):
        notes["data_gaps"] = {
            "gaps_remaining": list(bundle.gaps_remaining or [])[:20],
            "data_complete": bundle.data_complete,
        }
        phase_now = notes.get("lifecycle_phase") or LIFECYCLE_ENTRY_NOT_REACHED
        if phase_now == LIFECYCLE_ENTRY_NOT_REACHED:
            notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
    else:
        _clear_resolved_tracking_errors(notes, bundle)

    _save_notes(setup, notes)


def _advance_tracking_watermark(
    setup: AiProposedSetup,
    verified: list[Candle],
    bundle: KlineBundle | None,
    from_ms: int,
) -> None:
    notes = _notes(setup)
    if bundle and (bundle.gaps_remaining or not bundle.data_complete):
        if bundle.gaps_remaining:
            notes["pending_tracking_from_ms"] = min(bundle.gaps_remaining)
        else:
            notes["pending_tracking_from_ms"] = from_ms
        notes["missing_intervals"] = list(bundle.gaps_remaining or [])[:20]
        phase_now = notes.get("lifecycle_phase") or LIFECYCLE_ENTRY_NOT_REACHED
        if phase_now == LIFECYCLE_ENTRY_NOT_REACHED:
            notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
        _save_notes(setup, notes)
    else:
        _clear_resolved_tracking_errors(notes, bundle)
        _save_notes(setup, notes)
    if verified:
        setup.last_tracked_candle_ms = max(c.time_ms for c in verified)


async def _fetch_setup_tracking_candles(
    market: MarketDataService,
    symbol: str,
    setup: AiProposedSetup,
) -> tuple[list[Candle], KlineBundle | None, int]:
    step = interval_ms("5m")
    start_ms = _tracking_start_ms(setup)
    notes = _notes(setup)
    pending = notes.get("pending_tracking_from_ms")
    if setup.last_tracked_candle_ms:
        from_ms = setup.last_tracked_candle_ms + step
    else:
        from_ms = start_ms
    if pending is not None:
        from_ms = min(from_ms, int(pending))

    end_ms: int | None = None
    if setup.expires_at:
        end_ms = int(_ensure_utc(setup.expires_at).timestamp() * 1000)

    try:
        bundle = await market.fetch_historical_closed(symbol, "5m", from_ms, max_bars=2000)
    except Exception as exc:
        notes = _notes(setup)
        notes["fetch_error"] = str(exc)[:300]
        notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
        notes["pending_tracking_from_ms"] = from_ms
        notes["outcome_tracking_status"] = TRACKING_INCOMPLETE
        _save_notes(setup, notes)
        return [], None, from_ms

    scoped: list[Candle] = []
    for c in bundle.closed:
        if c.time_ms < from_ms or c.time_ms < start_ms:
            continue
        if end_ms is not None:
            if _candle_straddles_expiry(setup, c, "5m"):
                # Pass through to apply_objective_price_tracking for uncertainty flags only.
                scoped.append(c)
                continue
            if c.time_ms >= end_ms:
                continue
            if not _candle_fully_eligible_before_expiry(setup, c, "5m"):
                continue
        scoped.append(c)
    return scoped, bundle, from_ms


async def refresh_setup_objective_state(
    session: AsyncSession,
    *,
    symbol: str,
    candles_by_interval: dict[str, list[Candle]],
    market: MarketDataService | None = None,
) -> list[AiProposedSetup]:
    del candles_by_interval  # setup tracking uses historical fetch only (no incomplete fallback)
    market = market or MarketDataService()
    now = _utc()
    setups = await load_trackable_setups(session, symbol)
    for setup in setups:
        watermark = setup.last_tracked_candle_ms or 0
        scoped, bundle, from_ms = await _fetch_setup_tracking_candles(market, symbol, setup)
        verified = _verified_contiguous_prefix(scoped, bundle)
        apply_objective_price_tracking(setup, verified, bundle=bundle, watermark_ms=watermark)
        _advance_tracking_watermark(setup, verified, bundle, from_ms)
        _finalize_validity_and_tracking(setup, now)
    await session.flush()
    return await load_open_setups(session, symbol)


def _parse_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        f = float(val)
        if not math.isfinite(f) or f <= 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def validate_proposed_setup_levels(direction: str, entry: float, sl: float, tp: float) -> tuple[bool, str | None]:
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


def _levels_similar(a: float | None, b: float | None, ref: float) -> bool:
    if a is None or b is None:
        return False
    tol = ref * (LEVEL_DEDUPE_TOLERANCE_PCT / 100.0)
    return abs(a - b) <= tol


def _levels_materially_different(
    row: AiProposedSetup,
    entry: float,
    sl: float,
    tp: float,
) -> bool:
    ref = entry
    if row.entry_price is not None and _levels_similar(row.entry_price, entry, ref):
        sl_same = row.stop_loss is not None and _levels_similar(row.stop_loss, sl, ref)
        tp_same = row.take_profit is not None and _levels_similar(row.take_profit, tp, ref)
        return not (sl_same and tp_same)
    return True


async def _find_duplicate_open(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
    entry: float,
) -> AiProposedSetup | None:
    opens = await load_open_setups(session, symbol)
    for row in opens:
        if (row.direction or "").upper() != direction.upper():
            continue
        if row.entry_price is not None and _levels_similar(row.entry_price, entry, entry):
            return row
    return None


async def create_setup_from_structured(
    session: AsyncSession,
    *,
    run_id: int,
    symbol: str,
    structured: dict[str, Any],
) -> tuple[AiProposedSetup | None, dict[str, Any]]:
    """Returns (row or None, persistence_meta for artifacts)."""
    meta: dict[str, Any] = {"created": False, "updated": False, "skipped": False}
    decision = str(structured.get("decision", "NO_TRADE")).upper()
    if decision not in ("LONG", "SHORT"):
        meta["skipped"] = True
        meta["reason"] = "not_trade_decision"
        return None, meta

    proposed = structured.get("proposed_setup") if isinstance(structured.get("proposed_setup"), dict) else {}
    continue_id = proposed.get("continue_setup_id") or structured.get("continue_setup_id")
    new_setup = proposed.get("new_setup")
    if new_setup is None and not continue_id:
        new_setup = proposed.get("is_new", False)

    entry = _parse_float(proposed.get("entry_price") or structured.get("entry_price"))
    sl = _parse_float(proposed.get("stop_loss") or structured.get("stop_loss"))
    tp = _parse_float(proposed.get("take_profit") or structured.get("take_profit"))
    if entry is None or sl is None or tp is None:
        meta["skipped"] = True
        meta["reason"] = "missing_finite_levels"
        return None, meta

    ok, err = validate_proposed_setup_levels(decision, entry, sl, tp)
    if not ok:
        meta["skipped"] = True
        meta["reason"] = err
        return None, meta

    expires_hours = proposed.get("expires_hours") or structured.get("expires_hours") or 48
    try:
        expires_hours = float(expires_hours)
    except (TypeError, ValueError):
        expires_hours = 48.0
    expires_hours = max(MIN_EXPIRES_HOURS, min(MAX_EXPIRES_HOURS, expires_hours))

    if continue_id:
        existing = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == str(continue_id)))
        ).scalar_one_or_none()
        if not existing or existing.status not in ("open", "expired"):
            meta["skipped"] = True
            meta["reason"] = "continue_setup_not_found"
            return None, meta
        if existing.symbol != symbol or (existing.direction or "").upper() != decision:
            meta["skipped"] = True
            meta["reason"] = "continue_setup_mismatch"
            return None, meta
        _store_original_forecast(existing)
        _append_revision(existing, source_run_id=run_id, structured=structured, proposed=proposed, kind="continue")
        meta["updated"] = True
        meta["setup_id"] = existing.setup_id
        meta["reason"] = "revision_appended"
        await session.flush()
        return existing, meta

    dup = await _find_duplicate_open(session, symbol=symbol, direction=decision, entry=entry)
    if dup and not _levels_materially_different(dup, entry, sl, tp):
        _store_original_forecast(dup)
        _append_revision(dup, source_run_id=run_id, structured=structured, proposed=proposed, kind="dedupe")
        meta["updated"] = True
        meta["setup_id"] = dup.setup_id
        meta["reason"] = "deduped_revision_appended"
        await session.flush()
        return dup, meta

    if not new_setup:
        meta["skipped"] = True
        meta["reason"] = "new_setup_not_declared"
        return None, meta

    if dup and not proposed.get("distinct_new_setup"):
        meta["skipped"] = True
        meta["reason"] = "duplicate_open_requires_distinct_new_setup"
        return None, meta

    row = AiProposedSetup(
        setup_id=str(uuid.uuid4()),
        run_id=run_id,
        symbol=symbol,
        direction=decision,
        status="open",
        thesis_fa=proposed.get("thesis_fa") or structured.get("thesis_fa") or structured.get("structure_summary"),
        entry_price=entry,
        entry_zone_low=_parse_float(proposed.get("entry_zone_low") or structured.get("entry_zone_low")),
        entry_zone_high=_parse_float(proposed.get("entry_zone_high") or structured.get("entry_zone_high")),
        stop_loss=sl,
        take_profit=tp,
        invalidation=proposed.get("invalidation") or structured.get("invalidation"),
        invalidation_level=_parse_float(proposed.get("invalidation_level") or structured.get("invalidation_level")),
        expires_at=_utc() + timedelta(hours=expires_hours),
        structured_snapshot=structured,
        objective_notes={
            "disclaimer": "Touches are objective price observations, not paper fills.",
            "lifecycle_phase": LIFECYCLE_ENTRY_NOT_REACHED,
            "validity_status": VALIDITY_ACTIVE,
            "outcome_tracking_status": TRACKING_INCOMPLETE,
        },
        last_tracked_candle_ms=0,
    )
    session.add(row)
    await session.flush()
    _store_original_forecast(row)
    meta["created"] = True
    meta["setup_id"] = row.setup_id
    return row, meta


async def build_ai_memory_context(
    session: AsyncSession,
    *,
    symbol: str,
    candles_by_interval: dict[str, list[Candle]],
    market: MarketDataService | None = None,
) -> dict[str, Any]:
    setups = await refresh_setup_objective_state(
        session, symbol=symbol, candles_by_interval=candles_by_interval, market=market
    )
    trackable = await load_trackable_setups(session, symbol)
    reconciling = [s for s in trackable if (s.objective_notes or {}).get("validity_status") == VALIDITY_EXPIRED]
    closed = await load_recent_closed_setups(session, symbol)
    recent = await load_recent_analysis_summaries(session)
    return {
        "symbol": symbol,
        "open_setups": [serialize_setup_row(s) for s in setups],
        "reconciling_expired_setups": [serialize_setup_row(s) for s in reconciling if s not in setups],
        "recent_closed_setups": [serialize_setup_row(s) for s in closed],
        "recent_completed_analyses": recent,
        "memory_rules": [
            "Review open and recently closed setups BEFORE forming a new view.",
            "In report_fa, start with follow-up on prior setups (objective lifecycle_phase and first_touch_ms).",
            "Never claim a trade was executed unless paper_fill_recorded is true.",
            "SL/TP before entry are not post-entry outcomes.",
            "original_forecast is immutable; revision_history holds later AI assessments.",
        ],
    }
