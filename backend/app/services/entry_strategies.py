"""Strategy selection, settings and fail-closed contracts for one shared AI call."""
from __future__ import annotations

import math
from typing import Any

from app.services.settings_store import get_plain, set_plain

A = "ict_reversal"
B = "trend_continuation"
C = "breakout_retest"
LABELS = {A: "A · برگشت ICT", B: "B · ادامهٔ روند", C: "C · شکست و بازآزمایی"}
PROFILES = {B: "trend_pullback_resume", C: "level_breakout_retest"}
SEQUENCES = {
    "trend_pullback_resume": ["trend_pullback", "trend_resume", "entry_zone_touch"],
    "level_breakout_retest": ["breakout_close", "breakout_retest", "entry_zone_touch"],
}


def strategy_id(data: dict) -> str:
    # Historical snapshots have no strategy_id and keep their existing ICT behavior.
    return data.get("strategy_id", A)


async def strategy_settings(session) -> dict[str, bool]:
    return {key: (await get_plain(session, "entry_strategies", key, "true")) == "true" for key in LABELS}


async def save_strategy_settings(session, values: dict[str, bool]) -> None:
    if set(values) != set(LABELS) or not any(values.values()):
        raise ValueError("at_least_one_strategy_required")
    for key, enabled in values.items():
        await set_plain(session, "entry_strategies", key, "true" if enabled else "false")


async def strategy_enabled_error(session, data: dict) -> str | None:
    sid = strategy_id(data)
    if not isinstance(sid, str) or sid not in LABELS:
        return "unknown_entry_strategy"
    if not (await strategy_settings(session))[sid]:
        return "entry_strategy_disabled"
    return None


def validate_strategy_plan(data: dict[str, Any]) -> str | None:
    sid = strategy_id(data)
    if not isinstance(sid, str) or sid not in LABELS:
        return "unknown_entry_strategy"
    profile = data.get("confirmation_type")
    if sid == A:
        if (isinstance(profile, str) and profile in SEQUENCES) or data.get("confirmation_sequence") in list(SEQUENCES.values()):
            return "strategy_profile_mismatch"
        return None
    # Flat NO_TRADE has no entry plan to validate.
    if data.get("setup_mode") == "flat" and data.get("decision") == "NO_TRADE":
        return None
    if data.get("setup_mode") != "conditional" or data.get("final_ai_decision") != "WAIT" or data.get("decision") != "NO_TRADE":
        return "new_strategy_requires_objective_monitor"
    direction = data.get("potential_direction")
    if direction not in ("LONG", "SHORT"):
        return "missing_potential_direction"
    if profile != PROFILES[sid]:
        return "strategy_profile_mismatch"
    if "confirmation_sequence" in data and data["confirmation_sequence"] != SEQUENCES[profile]:
        return "strategy_profile_mismatch"
    regime = data.get("market_regime")
    expected = "bullish" if direction == "LONG" else "bearish"
    if sid == B and (regime != expected or data.get("trade_against_dominant_trend") is True):
        return "continuation_requires_aligned_trend"
    try:
        lo = float(data.get("conditional_entry_zone_low") or data.get("entry_zone_low"))
        hi = float(data.get("conditional_entry_zone_high") or data.get("entry_zone_high"))
        sl = float(data.get("stop_loss"))
        tp = float(data.get("take_profit"))
        inv = float(data.get("structural_invalidation_level"))
        values = [lo, hi, sl, tp, inv]
        if sid == C:
            level = float(data.get("breakout_level"))
            values.append(level)
            if not lo <= level <= hi:
                return "breakout_level_outside_entry_zone"
        if not all(math.isfinite(v) and v > 0 for v in values) or lo >= hi:
            return "invalid_strategy_levels"
        if direction == "LONG":
            if not sl < lo < hi < tp or inv >= lo:
                return "invalid_strategy_levels"
            rr = (tp - hi) / (hi - sl)
        else:
            if not tp < lo < hi < sl or inv <= hi:
                return "invalid_strategy_levels"
            rr = (lo - tp) / (sl - lo)
        if rr < 2:
            return "strategy_rr_below_two"
    except (TypeError, ValueError, ZeroDivisionError):
        return "invalid_strategy_levels"
    return None


def shared_strategy_instructions(base: str, enabled: dict[str, bool]) -> str:
    if not enabled[B] and not enabled[C]:
        return base  # Exact original A instructions when B/C are switched off.
    start = base.index("## Valid LONG/SHORT gate")
    end = base.index("## Anti-scalp", start)
    base = base[:start] + """## Strategy-specific gates
A (ict_reversal): keep sweep/run, POI reaction, MSS/BOS and structural invalidation
requirements. Immediate A requires all four original *_clear flags; otherwise WAIT.
B (trend_continuation): aligned 4H/1H trend, a meaningful pullback zone, then a
closed execution candle resuming that trend. No reversal sweep or FVG is required.
Use confirmation_type=trend_pullback_resume. Backend verifies zone pullback, then
close beyond the previous candle high (LONG) / low (SHORT), then entry-zone touch.
C (breakout_retest): identify an established support/resistance price from supplied
candles; supply breakout_level and an entry zone containing that price. Use
confirmation_type=level_breakout_retest. Backend requires a close crossing that
fixed level in the trade direction, a LATER retest touching the level and closing
on the breakout side, then entry-zone touch. A wick-only break is insufficient.
B/C MUST use setup_mode=conditional, final_ai_decision=WAIT, decision=NO_TRADE.
Do not fabricate liquidity_event_clear or other ICT reversal flags for B/C.
All strategies require structural SL, credible targets and R:R >= 2 across the
ENTIRE entry zone. Existing net R:R/fees/risk limits still apply before paper orders.

""" + base[end:]
    base = base.replace("You are an ICT discretionary INTRADAY trader", "You are a multi-strategy discretionary INTRADAY trader")
    base = base.replace("sweep_mss_poi | sweep_bos_poi | bos_fvg_retest | mss_fvg_retest", "sweep_mss_poi | sweep_bos_poi | bos_fvg_retest | mss_fvg_retest | trend_pullback_resume | level_breakout_retest")
    base = base.replace("Never replace the required ICT sweep, POI, MSS/BOS, invalidation or R:R gates.", "Never replace the selected strategy's required confirmation, invalidation or R:R gates.")
    base = base.replace("Immediate active trade (decision LONG/SHORT", "For strategy A only: immediate active trade (decision LONG/SHORT")
    base = base.replace("Counter-trend (fade) setups are allowed only", "For strategy A, counter-trend (fade) setups are allowed only")
    base += """
MULTI-STRATEGY OUTPUT CONTRACT (strategy-specific rules above take precedence over
legacy ICT-only wording): Evaluate ALL enabled strategies in this SAME response.
Never make separate reports or requests per strategy. Keep ONE strongest credible
plan in the existing top-level fields for the report. Additionally return
strategy_candidates: an object keyed by EVERY enabled strategy other than that
top-level strategy_id. Each value is either null (no credible opportunity) or a
COMPLETE, self-contained setup object using the SAME fields and validation rules
as the top-level plan, including explicit strategy_id, proposed_setup, regime,
confirmation_type, direction, levels and invalidation. Never inherit fields from
another candidate. At most ONE plan per strategy. Do not include report_fa,
chart_overlays, _artifacts or nested strategy_candidates in candidates.
No forced trade if none is valid. Disabled strategies must not have candidates.
Each enabled strategy must be assessed even when another strategy has a valid setup.
Existing setups only constrain revisions WITHIN their own strategy. Other strategies
can independently propose and monitor opportunities; shared execution risk limits
still decide whether orders may open. Return strategy_id for the top-level plan.
Return strategy_assessments: one brief Persian reason per enabled strategy
key, explaining its candidate or absence (max 160 characters each).
Shared sizing, anti-scalp, repeat-loss protection, SL and net R:R safeguards apply to
all. Sweep/reversal and countertrend-reversal evidence apply to A, not B/C. B must
align with the higher-timeframe trend; C needs its own explicit broken level/retest.
B/C require fresh confirmations after monitoring starts; do not claim historical
confirmation allows immediate entry. Do not revise a pending A setup into B/C or
vice versa. Within each strategy, if its existing setup remains valid, retain it or choose WAIT.
Use conditional entry zone, stop_loss, take_profit and structural_invalidation_level
for B/C; breakout_level is additionally required for C. Use only supplied market data. Server strategy_features are descriptive, not entry permission. Use trend strength, pullback depth, candle body/ATR, relative volume and breakout distance/ATR as soft quality evidence; never invent missing values or force threshold gates.
Enabled strategies (do not select disabled strategies):
""" + ", ".join(key for key, on in enabled.items() if on)
    return base
