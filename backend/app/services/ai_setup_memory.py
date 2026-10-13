from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.models import AiAnalysisRun, AiProposedSetup


def _utc(dt: datetime | None = None) -> datetime:
    return dt or datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def serialize_setup_row(row: AiProposedSetup) -> dict[str, Any]:
    return {
        "setup_id": row.setup_id,
        "run_id": row.run_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "status": row.status,
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
        "sl_touched": row.sl_touched,
        "tp_touched": row.tp_touched,
        "sequencing_ambiguous": row.sequencing_ambiguous,
        "paper_fill_recorded": row.paper_fill_recorded,
        "objective_notes": row.objective_notes or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
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
    await session.flush()
    return [r for r in rows if r.status == "open"]


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


def apply_objective_price_tracking(setup: AiProposedSetup, candles: list[Candle]) -> None:
    """Objective touch flags only — never implies a paper fill was recorded."""
    if not candles:
        return
    direction = (setup.direction or "").upper()
    entry_lo, entry_hi = _zone_bounds(setup)
    sl = setup.stop_loss
    tp = setup.take_profit
    inv = setup.invalidation_level

    sl_hit = False
    tp_hit = False
    entry_hit = False
    inv_hit = False

    for c in candles:
        if entry_lo is not None and entry_hi is not None:
            if c.low <= entry_hi and c.high >= entry_lo:
                entry_hit = True
        if inv is not None:
            if direction == "LONG" and c.close < inv:
                inv_hit = True
            if direction == "SHORT" and c.close > inv:
                inv_hit = True
        if sl is not None:
            if direction == "LONG" and c.low <= sl:
                sl_hit = True
            if direction == "SHORT" and c.high >= sl:
                sl_hit = True
        if tp is not None:
            if direction == "LONG" and c.high >= tp:
                tp_hit = True
            if direction == "SHORT" and c.low <= tp:
                tp_hit = True

    if sl_hit and tp_hit:
        setup.sequencing_ambiguous = True
        notes = dict(setup.objective_notes or {})
        notes["sequencing"] = "SL and TP both touched in window — order unknown; not a recorded paper fill."
        setup.objective_notes = notes

    setup.entry_touched = setup.entry_touched or entry_hit
    setup.sl_touched = setup.sl_touched or sl_hit
    setup.tp_touched = setup.tp_touched or tp_hit

    if inv_hit and setup.status == "open":
        setup.status = "invalidated"
        setup.closed_at = _utc()
    elif setup.expires_at and _ensure_utc(setup.expires_at) < _utc() and setup.status == "open":
        setup.status = "expired"
        setup.closed_at = _utc()


async def refresh_setup_objective_state(
    session: AsyncSession,
    *,
    symbol: str,
    candles_by_interval: dict[str, list[Candle]],
) -> list[AiProposedSetup]:
    setups = await load_open_setups(session, symbol)
    track = candles_by_interval.get("5m") or candles_by_interval.get("1m") or []
    for setup in setups:
        since_ms = int(_ensure_utc(setup.created_at).timestamp() * 1000)
        scoped = [c for c in track if c.time_ms >= since_ms]
        apply_objective_price_tracking(setup, scoped)
    await session.flush()
    return setups


def _parse_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


async def create_setup_from_structured(
    session: AsyncSession,
    *,
    run_id: int,
    symbol: str,
    structured: dict[str, Any],
) -> AiProposedSetup | None:
    decision = str(structured.get("decision", "NO_TRADE")).upper()
    if decision not in ("LONG", "SHORT"):
        return None
    proposed = structured.get("proposed_setup") if isinstance(structured.get("proposed_setup"), dict) else structured
    expires_hours = proposed.get("expires_hours") or structured.get("expires_hours") or 48
    try:
        expires_hours = float(expires_hours)
    except (TypeError, ValueError):
        expires_hours = 48.0

    row = AiProposedSetup(
        setup_id=str(uuid.uuid4()),
        run_id=run_id,
        symbol=symbol,
        direction=decision,
        status="open",
        thesis_fa=proposed.get("thesis_fa") or structured.get("thesis_fa") or structured.get("structure_summary"),
        entry_price=_parse_float(proposed.get("entry_price") or structured.get("entry_price")),
        entry_zone_low=_parse_float(proposed.get("entry_zone_low") or structured.get("entry_zone_low")),
        entry_zone_high=_parse_float(proposed.get("entry_zone_high") or structured.get("entry_zone_high")),
        stop_loss=_parse_float(proposed.get("stop_loss") or structured.get("stop_loss")),
        take_profit=_parse_float(proposed.get("take_profit") or structured.get("take_profit")),
        invalidation=proposed.get("invalidation") or structured.get("invalidation"),
        invalidation_level=_parse_float(proposed.get("invalidation_level") or structured.get("invalidation_level")),
        expires_at=_utc() + timedelta(hours=expires_hours),
        structured_snapshot=structured,
        objective_notes={"disclaimer": "Touches are objective price observations, not paper fills."},
    )
    session.add(row)
    await session.flush()
    return row


async def build_ai_memory_context(
    session: AsyncSession,
    *,
    symbol: str,
    candles_by_interval: dict[str, list[Candle]],
) -> dict[str, Any]:
    setups = await refresh_setup_objective_state(session, symbol=symbol, candles_by_interval=candles_by_interval)
    recent = await load_recent_analysis_summaries(session)
    return {
        "symbol": symbol,
        "open_setups": [serialize_setup_row(s) for s in setups],
        "recent_completed_analyses": recent,
        "memory_rules": [
            "Review every open setup and recent analysis BEFORE forming a new view.",
            "In report_fa, start with follow-up on prior setups (what happened objectively).",
            "Never claim a trade was executed unless paper_fill_recorded is true.",
            "Price touches on SL/TP/entry are observations only.",
        ],
    }
