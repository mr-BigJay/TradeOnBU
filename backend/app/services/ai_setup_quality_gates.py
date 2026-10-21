"""Server-side quality gates for AI setup persistence (paper ICT, no extra LLM calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiProposedSetup, SimPosition
from app.services.ai_trader_report import _has_conditional_plan

FOUR_ICT_GATES = (
    "liquidity_event_clear",
    "poi_reaction_clear",
    "structural_confirmation_clear",
    "logical_invalidation_clear",
)

VALID_REGIMES = frozenset({"bullish", "bearish", "range", "unclear"})

CONTRARIAN_EVIDENCE_KEYS = (
    "liquidity_sweep",
    "structure_shift_mss_or_bos",
    "poi_reaction",
)


def _parse_bool(val: Any) -> bool | None:
    if val is True:
        return True
    if val is False:
        return False
    return None


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


def contrarian_ict_evidence(structured: dict[str, Any]) -> dict[str, Any]:
    raw = structured.get("contrarian_ict_evidence")
    return raw if isinstance(raw, dict) else {}


def contrarian_evidence_valid(structured: dict[str, Any]) -> bool:
    ev = contrarian_ict_evidence(structured)
    return all(_parse_bool(ev.get(k)) is True for k in CONTRARIAN_EVIDENCE_KEYS)


def setup_independence_documented(structured: dict[str, Any]) -> bool:
    block = structured.get("setup_independence")
    if not isinstance(block, dict):
        return False
    if not block.get("independent_from_prior_setup"):
        return False
    summary = str(block.get("new_evidence_summary_fa") or "").strip()
    prior = str(
        block.get("prior_setup_reference_fa")
        or block.get("prior_reference_fa")
        or block.get("what_invalidated_prior_fa")
        or ""
    ).strip()
    return len(summary) >= 16 and len(prior) >= 8


def enrich_structured_audit(structured: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    out = dict(structured)
    out["setup_quality_audit"] = audit
    return out


async def _recent_losing_sim_same_direction(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
    within_hours: float = 168.0,
) -> SimPosition | None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    row = (
        await session.execute(
            select(SimPosition)
            .where(SimPosition.symbol == symbol)
            .where(SimPosition.direction == direction.upper())
            .where(SimPosition.status == "closed")
            .where(SimPosition.updated_at >= cutoff)
            .order_by(SimPosition.updated_at.desc())
            .limit(5)
        )
    ).scalars().all()
    for pos in row:
        pnl = pos.net_pnl_usdt
        if pnl is not None and float(pnl) < 0:
            return pos
        reason = str(pos.close_reason or "").lower()
        if "sl" in reason or "stop" in reason:
            return pos
    return None


async def _open_same_direction_setups(
    session: AsyncSession,
    *,
    symbol: str,
    direction: str,
) -> list[AiProposedSetup]:
    from app.services.ai_setup_memory import load_open_setups

    opens = await load_open_setups(session, symbol)
    return [s for s in opens if (s.direction or "").upper() == direction.upper()]


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
        "contrarian_ict_evidence": contrarian_ict_evidence(structured),
        "setup_independence": structured.get("setup_independence"),
        "distinct_new_setup": proposed.get("distinct_new_setup"),
        "source_run_id_note": "run_id on setup row may change on revision; use revision_history + setup_id lineage.",
        "checks": checks,
    }


async def validate_setup_quality_gates(
    session: AsyncSession,
    *,
    symbol: str,
    structured: dict[str, Any],
    market_intelligence: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """
    Return (error_code, audit). None error means persist allowed.
    Conditional WAIT may keep initial *_clear=false; monitor confirms later.
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

    if not is_conditional and decision in ("LONG", "SHORT"):
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

    if direction and (against or (regime not in ("unclear", "range") and direction_opposes_regime(direction, regime))):
        if not contrarian_evidence_valid(structured) and not (
            not is_conditional and four_ict_gates_clear(structured)
        ):
            checks.append({"gate": "contrarian_ict_evidence", "passed": False})
            return "contrarian_without_validated_ict_evidence", build_setup_quality_audit(
                structured=structured,
                direction=direction,
                regime=regime,
                against_trend=True,
                checks=checks,
            )
    checks.append({"gate": "contrarian_ict_evidence", "passed": True})

    if direction:
        recent_loss = await _recent_losing_sim_same_direction(session, symbol=symbol, direction=direction)
        if recent_loss is not None:
            proposed = structured.get("proposed_setup") if isinstance(structured.get("proposed_setup"), dict) else {}
            distinct = bool(proposed.get("distinct_new_setup"))
            documented = setup_independence_documented(structured)
            if not (distinct and documented):
                checks.append(
                    {
                        "gate": "repeat_direction_after_loss",
                        "passed": False,
                        "prior_sim_position_id": recent_loss.public_id,
                        "prior_close_reason": recent_loss.close_reason,
                    }
                )
                return "repeat_direction_after_loss_requires_independence", build_setup_quality_audit(
                    structured=structured,
                    direction=direction,
                    regime=regime,
                    against_trend=against,
                    checks=checks,
                )
        checks.append({"gate": "repeat_direction_after_loss", "passed": True})

        open_same = await _open_same_direction_setups(session, symbol=symbol, direction=direction)
        if len(open_same) >= 1:
            proposed = structured.get("proposed_setup") if isinstance(structured.get("proposed_setup"), dict) else {}
            if not proposed.get("distinct_new_setup") or not setup_independence_documented(structured):
                checks.append(
                    {
                        "gate": "parallel_same_direction",
                        "passed": False,
                        "open_setup_ids": [s.setup_id for s in open_same[:3]],
                    }
                )
                return "parallel_same_direction_requires_independence", build_setup_quality_audit(
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
