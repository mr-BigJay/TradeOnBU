from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import interval_ms
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


def serialize_setup_row(row: AiProposedSetup) -> dict[str, Any]:
    notes = row.objective_notes or {}
    return {
        "setup_id": row.setup_id,
        "run_id": row.run_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "status": row.status,
        "lifecycle_phase": notes.get("lifecycle_phase", LIFECYCLE_ENTRY_NOT_REACHED),
        "thesis_fa": row.thesis_fa,
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


async def load_open_setups(session: AsyncSession, symbol: str) -> list[AiProposedSetup]:
    now = _utc()
    rows = (
        await session.execute(
            select(AiProposedSetup)
            .where(AiProposedSetup.symbol == symbol)
            .where(AiProposedSetup.status == "open")
            .order_by(desc(AiProposedSetup.created_at))
            .limit(20)
        )
    ).scalars().all()
    for row in rows:
        if row.expires_at and _ensure_utc(row.expires_at) < now:
            row.status = "expired"
            row.closed_at = now
            notes = _notes(row)
            notes["lifecycle_phase"] = LIFECYCLE_EXPIRED
            _save_notes(row, notes)
    await session.flush()
    return [r for r in rows if r.status == "open"]


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
    return list(rows)


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
) -> None:
    """Chronological objective touches — never implies a paper fill."""
    if not candles:
        if bundle and (bundle.gaps_remaining or not bundle.data_complete):
            notes = _notes(setup)
            notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
            notes["tracking_warning"] = "No candles in window; history may be incomplete."
            _save_notes(setup, notes)
        return

    direction = (setup.direction or "").upper()
    entry_lo, entry_hi = _zone_bounds(setup)
    sl = setup.stop_loss
    tp = setup.take_profit
    inv = setup.invalidation_level
    notes = _notes(setup)
    notes.setdefault("disclaimer", "Touches are objective price observations, not paper fills.")
    phase = notes.get("lifecycle_phase", LIFECYCLE_ENTRY_NOT_REACHED)
    entry_reached = setup.entry_touched or phase in (
        LIFECYCLE_ENTRY_TOUCHED,
        LIFECYCLE_SL_AFTER_ENTRY,
        LIFECYCLE_TP_AFTER_ENTRY,
        LIFECYCLE_SAME_CANDLE_AMBIGUOUS,
    )

    sorted_candles = sorted(candles, key=lambda c: c.time_ms)
    for c in sorted_candles:
        if inv is not None:
            if direction == "LONG" and c.close < inv:
                setup.status = "invalidated"
                setup.closed_at = _utc()
                notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                _record_first_touch(notes, "invalidation", c.time_ms)
                _save_notes(setup, notes)
                return
            if direction == "SHORT" and c.close > inv:
                setup.status = "invalidated"
                setup.closed_at = _utc()
                notes["lifecycle_phase"] = LIFECYCLE_INVALIDATED
                _record_first_touch(notes, "invalidation", c.time_ms)
                _save_notes(setup, notes)
                return

        entry_hit = _touch_entry(c, entry_lo, entry_hi)
        sl_hit = _touch_sl(c, direction, sl)
        tp_hit = _touch_tp(c, direction, tp)

        if not entry_reached:
            if entry_hit and (sl_hit or tp_hit):
                setup.sequencing_ambiguous = True
                setup.entry_touched = True
                entry_reached = True
                notes["lifecycle_phase"] = LIFECYCLE_SAME_CANDLE_AMBIGUOUS
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
            elif phase == LIFECYCLE_ENTRY_NOT_REACHED and not sl_hit and not tp_hit:
                notes["lifecycle_phase"] = LIFECYCLE_ENTRY_NOT_REACHED
            continue

        if sl_hit:
            _record_first_touch(notes, "sl_post_entry", c.time_ms)
            setup.sl_touched = True
            notes["lifecycle_phase"] = LIFECYCLE_SL_AFTER_ENTRY
        if tp_hit:
            _record_first_touch(notes, "tp_post_entry", c.time_ms)
            setup.tp_touched = True
            if notes.get("lifecycle_phase") != LIFECYCLE_SL_AFTER_ENTRY:
                notes["lifecycle_phase"] = LIFECYCLE_TP_AFTER_ENTRY

    if bundle and (bundle.gaps_remaining or not bundle.data_complete):
        notes["data_gaps"] = {
            "gaps_remaining": len(bundle.gaps_remaining or []),
            "data_complete": bundle.data_complete,
        }
        if notes.get("lifecycle_phase") == LIFECYCLE_ENTRY_NOT_REACHED and not sorted_candles:
            notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA

    _save_notes(setup, notes)


async def _fetch_setup_tracking_candles(
    market: MarketDataService,
    symbol: str,
    setup: AiProposedSetup,
) -> tuple[list[Candle], KlineBundle | None]:
    created_ms = int(_ensure_utc(setup.created_at).timestamp() * 1000)
    step = interval_ms("5m")
    from_ms = setup.last_tracked_candle_ms + step if setup.last_tracked_candle_ms else created_ms
    now = _utc()
    if setup.expires_at and _ensure_utc(setup.expires_at) < now:
        return [], None
    try:
        bundle = await market.fetch_historical_closed(symbol, "5m", from_ms, max_bars=2000)
    except Exception as exc:
        notes = _notes(setup)
        notes["fetch_error"] = str(exc)[:300]
        notes["lifecycle_phase"] = LIFECYCLE_UNKNOWN_MISSING_DATA
        _save_notes(setup, notes)
        return [], None
    scoped = [c for c in bundle.closed if c.time_ms >= from_ms - step]
    return scoped, bundle


async def refresh_setup_objective_state(
    session: AsyncSession,
    *,
    symbol: str,
    candles_by_interval: dict[str, list[Candle]],
    market: MarketDataService | None = None,
) -> list[AiProposedSetup]:
    market = market or MarketDataService()
    setups = await load_open_setups(session, symbol)
    for setup in setups:
        scoped, bundle = await _fetch_setup_tracking_candles(market, symbol, setup)
        if not scoped and candles_by_interval:
            track = candles_by_interval.get("5m") or candles_by_interval.get("1m") or []
            since_ms = setup.last_tracked_candle_ms or int(_ensure_utc(setup.created_at).timestamp() * 1000)
            scoped = [c for c in track if c.time_ms >= since_ms]
            bundle = None
        apply_objective_price_tracking(setup, scoped, bundle=bundle)
        if scoped:
            setup.last_tracked_candle_ms = max(c.time_ms for c in scoped)
    await session.flush()
    return setups


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
        if existing and existing.status == "open":
            existing.run_id = run_id
            existing.thesis_fa = proposed.get("thesis_fa") or structured.get("thesis_fa") or existing.thesis_fa
            existing.structured_snapshot = structured
            meta["updated"] = True
            meta["setup_id"] = existing.setup_id
            await session.flush()
            return existing, meta
        meta["skipped"] = True
        meta["reason"] = "continue_setup_not_found"
        return None, meta

    if not new_setup:
        dup = await _find_duplicate_open(session, symbol=symbol, direction=decision, entry=entry)
        if dup:
            dup.run_id = run_id
            dup.thesis_fa = proposed.get("thesis_fa") or structured.get("thesis_fa") or dup.thesis_fa
            dup.structured_snapshot = structured
            meta["updated"] = True
            meta["setup_id"] = dup.setup_id
            meta["reason"] = "deduped_open_setup"
            await session.flush()
            return dup, meta
        meta["skipped"] = True
        meta["reason"] = "new_setup_not_declared"
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
        },
        last_tracked_candle_ms=0,
    )
    session.add(row)
    await session.flush()
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
    closed = await load_recent_closed_setups(session, symbol)
    recent = await load_recent_analysis_summaries(session)
    return {
        "symbol": symbol,
        "open_setups": [serialize_setup_row(s) for s in setups],
        "recent_closed_setups": [serialize_setup_row(s) for s in closed],
        "recent_completed_analyses": recent,
        "memory_rules": [
            "Review open and recently closed setups BEFORE forming a new view.",
            "In report_fa, start with follow-up on prior setups (objective lifecycle_phase and first_touch_ms).",
            "Never claim a trade was executed unless paper_fill_recorded is true.",
            "SL/TP before entry are not post-entry outcomes.",
            "To revise an open scenario use proposed_setup.continue_setup_id; only set new_setup true for a genuinely new setup.",
        ],
    }
