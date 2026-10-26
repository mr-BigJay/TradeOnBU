"""Server-side quality gates for AI setup persistence and READY paper orders (no extra LLM calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import candle_close_ms, is_candle_closed

from app.domain.candles import Candle
from app.domain.time_utils import ensure_utc
from app.models import AiProposedSetup, SimPosition
from app.services.ai_trader_report import _has_conditional_plan
from app.services.ict_confirmation_engine import _check_invalidation_on_bar, _token_on_bar

FOUR_ICT_GATES = (
    "liquidity_event_clear",
    "poi_reaction_clear",
    "structural_confirmation_clear",
    "logical_invalidation_clear",
)

VALID_REGIMES = frozenset({"bullish", "bearish", "range", "unclear"})

TERMINAL_SIM_STATUSES = frozenset({"closed", "sl_hit", "tp_hit", "liquidated", "cancelled"})

ACTIVE_SIM_STATUSES = frozenset({"pending_entry", "open"})


def _parse_bool(val: Any) -> bool | None:
    if val is True:
        return True
    if val is False:
        return False
    return None


def is_sim_position_loss(pos: SimPosition) -> bool:
    status = str(pos.status or "").lower()
    if status == "sl_hit":
        return True
    if status == "liquidated":
        return True
    pnl = pos.net_pnl_usdt
    if pnl is not None and float(pnl) < 0:
        return True
    reason = str(pos.close_reason or "").lower()
    return "sl" in reason or "stop" in reason


def is_sim_summary_loss(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    if status == "sl_hit":
        return True
    pnl = row.get("net_pnl_usdt")
    if pnl is not None and float(pnl) < 0:
        return True
    reason = str(row.get("close_reason") or "").lower()
    return "sl" in reason or "stop" in reason


def sim_position_failure_ms(pos: SimPosition) -> int:
    ts = pos.closed_at or pos.updated_at or datetime.now(timezone.utc)
    return int(ensure_utc(ts).timestamp() * 1000)


def parse_market_regime(structured: dict[str, Any]) -> str:
    raw = str(structured.get("market_regime") or "").strip().lower()
    if raw in VALID_REGIMES:
        return raw
    return "unclear"


def resolve_trade_direction(structured: dict[str, Any]) -> str | None:
    if _has_conditional_plan(structured):
        pot = str(structured.get("potential_direction") or "").upper()
        return pot if pot in ("LONG", "SHORT") else None
    decision = str(structured.get("decision") or "NO_TRADE").upper()
    return decision if decision in ("LONG", "SHORT") else None


def direction_opposes_regime(direction: str, regime: str) -> bool:
    direction = direction.upper()
    if regime == "bullish":
        return direction == "SHORT"
    if regime == "bearish":
        return direction == "LONG"
    return False


def four_ict_gates_clear(structured: dict[str, Any]) -> bool:
    return all(_parse_bool(structured.get(k)) is True for k in FOUR_ICT_GATES)


def enrich_structured_audit(structured: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    out = dict(structured)
    out["setup_quality_audit"] = audit
    return out


def merge_setup_quality_audit(notes: dict[str, Any], audit: dict[str, Any], *, run_id: int) -> None:
    notes["setup_quality_audit"] = audit
    history = notes.setdefault("quality_audit_history", [])
    if isinstance(history, list):
        history.append({"run_id": run_id, "audit": audit})


def pick_latest_closed_5m(closed_5m: list[Candle], *, now_ms: int) -> Candle | None:
    for bar in reversed(sorted(closed_5m, key=lambda c: c.time_ms)):
        if is_candle_closed(bar, "5m", now_ms):
            return bar
    return None


def pick_latest_closed_5m_as_of(closed_5m: list[Candle], *, as_of_ms: int) -> Candle | None:
    """Last 5m bar whose close time is at or before as_of_ms (event-time fill reconciliation)."""
    best: Candle | None = None
    best_close = -1
    for bar in closed_5m:
        close_ms = candle_close_ms(bar, "5m")
        if close_ms <= as_of_ms and close_ms > best_close:
            best = bar
            best_close = close_ms
    return best


def objective_sequence_evidence_after_ms(
    *,
    direction: str,
    sequence: list[str],
    closed_5m: list[Candle],
    after_ms: int,
    zone_lo: float,
    zone_hi: float,
    structured: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, int]]:
    """
    True when each sequence token first appears on a strictly later closed 5m open than after_ms
    and timestamps increase in sequence order (no look-ahead beyond supplied closed bars).
    """
    hits: dict[str, int] = {}
    last_ms = after_ms
    ordered = [t for t in sequence if t]
    struct = structured if isinstance(structured, dict) else {}
    for bar in sorted(closed_5m, key=lambda c: c.time_ms):
        if bar.time_ms <= after_ms:
            continue
        if len(hits) >= len(ordered):
            break
        tok = ordered[len(hits)]
        window = [c for c in closed_5m if c.time_ms <= bar.time_ms][-48:]
        if _token_on_bar(
            tok,
            direction=direction,
            window=window,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=struct,
        ):
            if bar.time_ms <= last_ms:
                return False, hits
            hits[tok] = bar.time_ms
            last_ms = bar.time_ms
    return len(hits) == len(ordered) and bool(hits), hits


FILL_EXECUTION_SUSPEND_REASONS = frozenset(
    {
        "market_data_unverified_for_fill",
        "missing_closed_5m_for_fill",
        "missing_closed_5m_as_of_for_fill",
    }
)


def is_fill_execution_suspend_reason(reason: str | None) -> bool:
    return bool(reason and reason in FILL_EXECUTION_SUSPEND_REASONS)


def confirmations_strictly_after_ms(setup: AiProposedSetup, after_ms: int) -> tuple[bool, dict[str, int]]:
    conf = setup.confirmation_state if isinstance(setup.confirmation_state, dict) else {}
    required = [t for t in (conf.get("required") or conf.get("sequence") or []) if t]
    ts_map = conf.get("confirmation_timestamps") if isinstance(conf.get("confirmation_timestamps"), dict) else {}
    if not required:
        return False, {}
    out: dict[str, int] = {}
    prev = after_ms
    for tok in required:
        raw = ts_map.get(tok)
        if raw is None:
            return False, out
        ms = int(raw)
        if ms <= after_ms or ms <= prev:
            return False, out
        out[tok] = ms
        prev = ms
    return True, out


async def _recent_losing_sim_same_direction(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
    within_hours: float = 168.0,
) -> SimPosition | None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    rows = (
        await session.execute(
            select(SimPosition)
            .where(SimPosition.symbol == symbol)
            .where(SimPosition.direction == direction.upper())
            .where(SimPosition.status.in_(tuple(TERMINAL_SIM_STATUSES)))
            .where(SimPosition.updated_at >= cutoff)
            .order_by(SimPosition.updated_at.desc())
            .limit(8)
        )
    ).scalars().all()
    for pos in rows:
        if is_sim_position_loss(pos):
            return pos
    return None


async def _open_same_direction_setups(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
    exclude_setup_ids: set[str] | None = None,
) -> list[AiProposedSetup]:
    from app.services.ai_setup_memory import load_open_setups

    exclude = exclude_setup_ids or set()
    opens = await load_open_setups(session, symbol)
    from app.domain.setup_lifecycle import ACTIVE_MONITOR_STATES

    return [
        s
        for s in opens
        if (s.direction or "").upper() == direction.upper()
        and s.setup_id not in exclude
        and s.lifecycle_state in ACTIVE_MONITOR_STATES
    ]


async def _active_sim_same_direction_excluding_setup(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
    setup_id: str,
    exclude_sim_public_id: str | None = None,
) -> SimPosition | None:
    filters = [
        SimPosition.symbol == symbol,
        SimPosition.direction == direction.upper(),
        SimPosition.status.in_(tuple(ACTIVE_SIM_STATUSES)),
        or_(SimPosition.ai_setup_id.is_(None), SimPosition.ai_setup_id != setup_id),
    ]
    if exclude_sim_public_id:
        filters.append(SimPosition.public_id != exclude_sim_public_id)
    row = (await session.execute(select(SimPosition).where(*filters).limit(1))).scalar_one_or_none()
    return row


def _market_intel_usable(market_intelligence: dict[str, Any] | None) -> bool:
    if not market_intelligence:
        return False
    return str(market_intelligence.get("status") or "").lower() == "available"


def build_setup_quality_audit(
    *,
    structured: dict[str, Any],
    direction: str | None,
    regime: str,
    against_trend: bool,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    proposed = structured.get("proposed_setup") if isinstance(structured.get("proposed_setup"), dict) else {}
    return {
        "market_regime": regime,
        "trade_against_dominant_trend": against_trend,
        "direction": direction,
        "initial_analysis_flags": {k: _parse_bool(structured.get(k)) for k in FOUR_ICT_GATES},
        "distinct_new_setup": proposed.get("distinct_new_setup"),
        "source_run_id_note": "run_id on setup row may change on revision; use revision_history + setup_id lineage.",
        "checks": checks,
    }


async def _load_closed_5m_for_evidence(session: AsyncSession, *, symbol: str, limit: int = 72) -> list[Candle]:
    from app.config import get_effective_settings
    from app.services.market_data import MarketDataService

    settings = get_effective_settings()
    market = MarketDataService(settings)
    bundle = await market.fetch_kline_bundle(symbol, "5m", limit=limit)
    return list(bundle.closed or [])


async def validate_setup_quality_gates(
    session: AsyncSession,
    *,
    symbol: str,
    structured: dict[str, Any],
    market_intelligence: dict[str, Any] | None = None,
    exclude_setup_ids: set[str] | None = None,
    is_revision: bool = False,
) -> tuple[str | None, dict[str, Any]]:
    """
    Return (error_code, audit). None error means persist allowed.
    Conditional WAIT may keep initial *_clear=false; objective monitor proves before orders.
    """
    checks: list[dict[str, Any]] = []
    direction = resolve_trade_direction(structured)
    regime = parse_market_regime(structured)
    against = _parse_bool(structured.get("trade_against_dominant_trend"))
    if against is None and direction and regime not in ("unclear", "range"):
        against = direction_opposes_regime(direction, regime)
    against = bool(against)

    is_conditional = _has_conditional_plan(structured)
    decision = str(structured.get("decision") or "NO_TRADE").upper()
    is_immediate_entry = not is_conditional and decision in ("LONG", "SHORT")

    if direction and regime != "unclear" and not _market_intel_usable(market_intelligence):
        conf = str(structured.get("market_regime_confidence") or "").lower()
        if conf in ("high", "medium") and not _parse_bool(structured.get("market_context_caution_ack")):
            checks.append({"gate": "market_context_stale", "passed": False})
            return "market_context_stale_for_regime_claim", build_setup_quality_audit(
                structured=structured,
                direction=direction,
                regime=regime,
                against_trend=against,
                checks=checks,
            )
    checks.append({"gate": "market_context_stale", "passed": True})

    if is_immediate_entry:
        if not four_ict_gates_clear(structured):
            checks.append({"gate": "immediate_four_ict_gates", "passed": False})
            return "immediate_four_ict_gates_incomplete", build_setup_quality_audit(
                structured=structured,
                direction=direction,
                regime=regime,
                against_trend=against,
                checks=checks,
            )
    checks.append({"gate": "immediate_four_ict_gates", "passed": True})

    # Counter-trend: enforced only for immediate entry attempts, not conditional WAIT at creation.
    if is_immediate_entry and direction and (
        against or (regime not in ("unclear", "range") and direction_opposes_regime(direction, regime))
    ):
        if not four_ict_gates_clear(structured):
            checks.append({"gate": "contrarian_immediate_requires_full_gates", "passed": False})
            return "contrarian_immediate_four_gates_incomplete", build_setup_quality_audit(
                structured=structured,
                direction=direction,
                regime=regime,
                against_trend=True,
                checks=checks,
            )
    checks.append({"gate": "contrarian_immediate_requires_full_gates", "passed": True})

    recent_loss = (
        await _recent_losing_sim_same_direction(session, symbol=symbol, direction=direction)
        if direction
        else None
    )
    if direction and recent_loss is not None:
        failure_ms = sim_position_failure_ms(recent_loss)
        checks.append(
            {
                "gate": "repeat_direction_after_loss",
                "prior_sim_position_id": recent_loss.public_id,
                "prior_status": recent_loss.status,
                "prior_close_reason": recent_loss.close_reason,
                "failure_ms": failure_ms,
            }
        )
        if is_immediate_entry:
            from app.services.conditional_setup import resolve_immediate_confirmation_sequence

            sequence, _ = resolve_immediate_confirmation_sequence(structured)
            zone_lo = float(structured.get("entry_zone_low") or structured.get("conditional_entry_zone_low") or 0)
            zone_hi = float(structured.get("entry_zone_high") or structured.get("conditional_entry_zone_high") or 0)
            if zone_lo <= 0 or zone_hi <= 0:
                entry = structured.get("entry_price")
                if entry:
                    zone_lo = float(entry) * 0.999
                    zone_hi = float(entry) * 1.001
            closed_5m = await _load_closed_5m_for_evidence(session, symbol=symbol)
            ok_struct, hit_ms = objective_sequence_evidence_after_ms(
                direction=direction,
                sequence=list(sequence or []),
                closed_5m=closed_5m,
                after_ms=failure_ms,
                zone_lo=zone_lo,
                zone_hi=zone_hi,
                structured=structured,
            )
            checks[-1]["objective_hits_after_failure"] = hit_ms
            if not ok_struct:
                checks[-1]["passed"] = False
                return "repeat_direction_no_objective_structure_since_loss", build_setup_quality_audit(
                    structured=structured,
                    direction=direction,
                    regime=regime,
                    against_trend=against,
                    checks=checks,
                )
        else:
            checks[-1]["passed"] = True
            checks[-1]["deferred_to_monitor"] = True
    else:
        checks.append({"gate": "repeat_direction_after_loss", "passed": True})

    open_same = (
        await _open_same_direction_setups(
            session,
            symbol=symbol,
            direction=direction,
            exclude_setup_ids=exclude_setup_ids,
        )
        if direction
        else []
    )
    # Monitoring opportunities are independent; execution remains portfolio-wide.
    from app.services.entry_strategies import strategy_id
    open_same = [s for s in open_same if strategy_id(s.structured_snapshot or {}) == strategy_id(structured)]
    if direction and open_same and not is_revision:
        checks.append(
            {
                "gate": "parallel_same_direction",
                "passed": False,
                "open_setup_ids": [s.setup_id for s in open_same[:3]],
            }
        )
        return "parallel_same_direction_use_revision", build_setup_quality_audit(
            structured=structured,
            direction=direction,
            regime=regime,
            against_trend=against,
            checks=checks,
        )
    checks.append({"gate": "parallel_same_direction", "passed": True})

    return None, build_setup_quality_audit(
        structured=structured,
        direction=direction,
        regime=regime,
        against_trend=against,
        checks=checks,
    )


async def validate_setup_quality_at_execution(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    latest_closed_5m: Candle | None,
    market_data_verified: bool,
    now_ms: int,
    phase: str = "order",
    exclude_sim_public_id: str | None = None,
    closed_5m: list[Candle] | None = None,
    as_of_ms: int | None = None,
) -> tuple[bool, str | None]:
    """
    Re-validate on closed 5m data before paper order creation or limit fill (no look-ahead).
    Does not use stale AI market_regime — only structural invalidation, objective confirmation
    timestamps vs prior loss, expiry, and same-direction sim stacking.
    """
    if not market_data_verified:
        return False, f"market_data_unverified_for_{phase}"

    struct_bar: Candle | None
    if phase == "fill":
        bars = list(closed_5m or [])
        if not bars:
            return False, f"missing_closed_5m_for_{phase}"
        struct_bar = pick_latest_closed_5m_as_of(bars, as_of_ms=as_of_ms if as_of_ms is not None else now_ms)
        if struct_bar is None:
            return False, "missing_closed_5m_as_of_for_fill"
    else:
        struct_bar = latest_closed_5m
        if struct_bar is None:
            return False, f"missing_latest_closed_5m_for_{phase}"
        if not is_candle_closed(struct_bar, "5m", now_ms):
            return False, f"latest_5m_not_closed_at_{phase}"

    direction = (setup.direction or "").upper()
    inv = setup.invalidation_level
    if inv is not None and struct_bar is not None:
        if _check_invalidation_on_bar(struct_bar, direction=direction, invalidation_level=float(inv)):
            return False, "structural_invalidation_level_breached"

    loss = await _recent_losing_sim_same_direction(session, symbol=setup.symbol, direction=direction)
    if loss is not None:
        failure_ms = sim_position_failure_ms(loss)
        ok, _ts = confirmations_strictly_after_ms(setup, failure_ms)
        if not ok:
            return False, "confirmations_not_after_prior_same_direction_loss"

    other = await _active_sim_same_direction_excluding_setup(
        session,
        symbol=setup.symbol,
        direction=direction,
        setup_id=setup.setup_id,
        exclude_sim_public_id=exclude_sim_public_id,
    )
    if other is not None:
        return False, "stacked_same_direction_paper_position"

    exp = ensure_utc(setup.expires_at)
    if exp is not None and now_ms >= int(exp.timestamp() * 1000):
        return False, "setup_expired"

    return True, None


async def validate_ready_setup_before_paper_order(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    latest_closed_5m: Candle | None,
    now_ms: int,
    market_data_verified: bool = True,
) -> tuple[bool, str | None]:
    return await validate_setup_quality_at_execution(
        session,
        setup,
        latest_closed_5m=latest_closed_5m,
        market_data_verified=market_data_verified,
        now_ms=now_ms,
        phase="order",
    )

