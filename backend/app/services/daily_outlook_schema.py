from __future__ import annotations

import json
import math
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scenario(StrictModel):
    title_fa: str = Field(min_length=3, max_length=100)
    direction: Literal["bullish", "bearish", "range"]
    thesis_fa: str = Field(min_length=20, max_length=300)
    activation_fa: str = Field(min_length=10, max_length=220)
    invalidation_fa: str = Field(min_length=10, max_length=220)
    activation_level_ids: list[str] = Field(min_length=1, max_length=3)
    invalidation_level_ids: list[str] = Field(min_length=1, max_length=3)
    target_level_ids: list[str] = Field(default_factory=list, max_length=3)
    path_fa: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(min_length=2, max_length=2)
    path_level_ids: list[str] = Field(default_factory=list, max_length=6)
    retest_level_ids: list[str] = Field(default_factory=list, max_length=3)
    target_unavailable_reason_fa: str | None = Field(default=None, max_length=400)
    target_zone_level_ids: list[str] = Field(default_factory=list, max_length=2)


class TradePlan(StrictModel):
    action: Literal["long", "short", "wait"]
    rationale_fa: str = Field(min_length=10, max_length=250)
    trigger_fa: str = Field(min_length=10, max_length=250)
    entry_level_id: str | None
    stop_level_id: str | None
    target_level_ids: list[str] = Field(max_length=2)

    @model_validator(mode="after")
    def complete_plan(self):
        if self.action == "wait":
            if self.entry_level_id is not None or self.stop_level_id is not None or self.target_level_ids:
                raise ValueError("wait_plan_must_not_have_prices")
        elif not self.entry_level_id or not self.stop_level_id or not self.target_level_ids:
            raise ValueError("incomplete_trade_plan")
        return self


class SupplementalEvidence(StrictModel):
    source: Literal["funding", "open_interest", "trade_flow", "order_book", "liquidations"]
    effect: Literal["supports", "opposes", "neutral", "not_assessed"]
    explanation_fa: str = Field(min_length=5, max_length=180)


class CandleLevel(StrictModel):
    """AI-selected levels must point to a real supplied HTF candle extremum."""
    id: str = Field(pattern=r"^ai_[a-z0-9_]{1,48}$")
    label_fa: str = Field(min_length=3, max_length=100)
    timeframe: Literal["1d", "1w"]
    candle_time_ms: int = Field(strict=True, ge=0)
    field: Literal["high", "low", "close"]
    price: float = Field(gt=0, allow_inf_nan=False)
    reason_fa: str = Field(min_length=10, max_length=240)


class TrendAssessment(StrictModel):
    phase: Literal["continuation", "correction", "possible_reversal", "range", "unclear"]
    phase_fa: str = Field(min_length=20, max_length=400)
    reversal_watch_fa: str = Field(min_length=20, max_length=400)
    expected_path_fa: str = Field(min_length=20, max_length=500)
    horizon_fa: str = Field(min_length=10, max_length=180)


class SwingPlan(TradePlan):
    entry_valid_hours: int = Field(default=24, ge=1, le=48)
    holding_days: int = Field(default=7, ge=2, le=14)
    structural_reason_fa: str = Field(min_length=10, max_length=300)


class PlanReview(StrictModel):
    prior_report_id: int = Field(gt=0)
    assessment_fa: str = Field(min_length=20, max_length=400)
    late_entry_fa: str = Field(min_length=20, max_length=400)


class Outlook(StrictModel):
    swing_plan: SwingPlan | None = None
    previous_plan_review: PlanReview | None = None
    trend_assessment: TrendAssessment | None = None
    analyst_levels: list[CandleLevel] = Field(default_factory=list, max_length=8)
    supplemental_evidence: list[SupplementalEvidence] = Field(default_factory=list, max_length=5)
    trade_plan: TradePlan
    summary_fa: str = Field(min_length=30, max_length=550)
    weekly_context_fa: str = Field(min_length=10, max_length=250)
    daily_structure_fa: str = Field(min_length=10, max_length=250)
    dominant: Scenario
    alternative: Scenario
    uncertainty_fa: str = Field(min_length=10, max_length=600)
    watch_next_fa: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(min_length=1, max_length=2)
    change_since_previous_fa: str = Field(min_length=5, max_length=500)

    @model_validator(mode="after")
    def different_scenarios(self):
        if self.dominant.direction == self.alternative.direction:
            raise ValueError("alternative_must_have_a_different_direction")
        return self


def parse_outlook(text: str, context: dict, *, execution_plan: dict | None = None) -> dict:
    raw = text.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    data = json.loads(raw)
    independent = context.get("analysis_mode") == "independent_htf_v1"
    if (execution_plan is not None or independent) and isinstance(data, dict):
        # The legacy execution card cannot be supplied by the model.
        data["trade_plan"] = dict(action="wait", rationale_fa="تأیید ستاپ در موتور ورود بررسی می‌شود.",
                                  trigger_fa="منتظر تأیید ساختار قیمت بمانید.",
                                  entry_level_id=None, stop_level_id=None, target_level_ids=[])
    result = Outlook.model_validate(data)
    levels = list(context["levels"])
    if independent and result.trend_assessment is None:
        raise ValueError("missing_trend_assessment")
    if result.analyst_levels and not independent:
        raise ValueError("unexpected_analyst_levels")
    for selected in result.analyst_levels:
        if selected.id in {level["id"] for level in levels}:
            raise ValueError("duplicate_analyst_level")
        rows = (context.get("candles") or {}).get(selected.timeframe, [])
        candle = next((r for r in rows if r[0] == selected.candle_time_ms), None)
        column = {"high": 2, "low": 3, "close": 4}[selected.field]
        if candle is None or len(candle) <= column:
            raise ValueError("unknown_analyst_candle")
        actual = float(candle[column])
        if not math.isfinite(actual) or actual <= 0 or not math.isclose(selected.price, actual, rel_tol=0, abs_tol=0.000001):
            raise ValueError("ungrounded_analyst_price")
        levels.append({**selected.model_dump(), "price": actual, "source": "ai_selected_closed_candle"})
    allowed = {level["id"] for level in levels}
    for scenario in (result.dominant, result.alternative):
        ids = scenario.activation_level_ids + scenario.invalidation_level_ids + scenario.target_level_ids + scenario.path_level_ids + scenario.target_zone_level_ids + scenario.retest_level_ids
        if scenario.target_level_ids and scenario.path_level_ids and (len(scenario.path_level_ids) < 2 or scenario.path_level_ids[-1] not in scenario.target_level_ids):
            raise ValueError("invalid_chart_path")
        if not set(ids) <= allowed:
            raise ValueError("unknown_evidence_level")
        if not independent and not scenario.target_level_ids:
            raise ValueError("missing_scenario_target")
        if not scenario.target_level_ids and (scenario.path_level_ids or scenario.target_zone_level_ids):
            raise ValueError("targetless_scenario_has_target_path")
        if independent and not scenario.target_level_ids and not scenario.target_unavailable_reason_fa:
            raise ValueError("missing_target_unavailable_reason")
        if scenario.target_zone_level_ids:
            prices = {level["id"]: level["price"] for level in levels}
            bounds = [prices[key] for key in scenario.target_zone_level_ids]
            destination = prices[(scenario.path_level_ids or scenario.target_level_ids)[-1]]
            if len(bounds) != 2 or bounds[0] >= bounds[1] or not bounds[0] <= destination <= bounds[1]:
                raise ValueError("invalid_target_zone")
        if any(not stage.strip() or len(stage) > 180 for stage in scenario.path_fa):
            raise ValueError("invalid_scenario_stage")
    output = result.model_dump()
    if independent:
        prices = {level["id"]: level["price"] for level in levels}
        quote = context.get("live_price")
        for key in ("dominant", "alternative"):
            scenario = output[key]
            sign = {"bullish": 1, "bearish": -1}.get(scenario["direction"], 0)
            # Path waypoints may themselves be targets. Compare destinations with
            # activation levels, never with an intermediate destination.
            refs = scenario["activation_level_ids"]
            reference = max((prices[k] for k in refs), key=lambda p: sign * p) if refs else quote
            targets = [prices[k] for k in scenario["target_level_ids"]]
            bounds = [prices[k] for k in scenario["target_zone_level_ids"]]
            if sign and targets and (
                not isinstance(quote, (int, float)) or not math.isfinite(quote)
                or any(sign * (p - quote) <= 0 or sign * (p - reference) <= 0 for p in targets + bounds)
            ):
                # Preserve the narrative and raw response; never invent a replacement price.
                scenario.update(target_level_ids=[], target_zone_level_ids=[], path_level_ids=[],
                    target_unavailable_reason_fa="هدف پیشنهادی با جهت سناریو و قیمت مرجع سازگار نبود؛ هدف معتبر مشخص نشده است.",
                    target_validation="rejected_geometry")

    supplied = (context.get("market_intelligence") or {}).get("sources") or {}
    entries = {item.source: item.model_dump() for item in result.supplemental_evidence}
    if len(entries) != len(result.supplemental_evidence) or not set(entries) <= set(supplied):
        raise ValueError("invalid_supplemental_evidence_source")
    # Never imply the model assessed a source when it omitted it.
    output["supplemental_evidence"] = [entries.get(name, {
        "source": name, "effect": "not_assessed",
        "explanation_fa": "داده ارسال شد؛ اثر آن در پاسخ مدل توضیح داده نشده است."
    }) for name in ("funding", "open_interest", "trade_flow", "order_book", "liquidations") if name in supplied]
    if independent:
        review = result.previous_plan_review
        if review and review.prior_report_id not in {r["id"] for r in context.get("previous_outlooks", [])}:
            raise ValueError("unknown_previous_outlook")
        try:
            output["swing_plan"] = validate_swing_plan(result.swing_plan, levels, context)
        except ValueError as exc:
            reasons = {
                "unknown_swing_level": "سطح پیشنهادی در داده‌های معتبر یافت نشد.",
                "invalid_swing_geometry": "ترتیب ورود، حدضرر و اهداف معتبر نبود.",
                "swing_stop_too_tight": "فاصلهٔ حدضرر نسبت به نوسان روزانه کافی نیست یا دادهٔ نوسان موجود نیست.",
                "swing_already_invalidated": "قیمت از محدودهٔ اعتبار پیشنهاد گذشته یا قیمت تازه موجود نیست.",
                "swing_net_rr_below_minimum": "نسبت سود به زیان پس از هزینه‌های تخمینی کمتر از ۱٫۵ است.",
            }
            if str(exc) not in reasons:
                raise
            output["swing_plan"] = {"source": "independent_swing", "action": "wait",
                "execution_status": "not_tracked", "validation_reason": str(exc),
                "rationale_fa": reasons[str(exc)], "trigger_fa": "در گزارش بعدی دوباره ارزیابی می‌شود.",
                "entry_level_id": None, "stop_level_id": None, "target_level_ids": []}
        output["analysis_mode"] = "independent_htf_v1"
        output["trade_plan"] = dict(action="wait", source="outlook_only",
            rationale_fa="این گزارش مسیر چندروزه بازار را بررسی می‌کند و سیگنال اجرایی نیست.",
            trigger_fa="ورود روزانه به ارزیابی جداگانه در زمان ورود نیاز دارد.",
            entry_level_id=None, stop_level_id=None, target_level_ids=[])
        # Publish newly selected levels only after the complete report validates.
        context["levels"] = levels
        return output
    if execution_plan is not None:
        output["trade_plan"] = execution_plan
        return output
    plan = result.trade_plan
    if plan.action != "wait":
        prices = {level["id"]: float(level["price"]) for level in context["levels"]}
        ids = [plan.entry_level_id, plan.stop_level_id, *plan.target_level_ids]
        if not set(ids) <= allowed:
            raise ValueError("unknown_trade_plan_level")
        entry, stop, *targets = [prices[id] for id in ids]
        if not all(math.isfinite(p) and p > 0 for p in [entry, stop, *targets]):
            raise ValueError("invalid_trade_plan_price")
        sign = 1 if plan.action == "long" else -1
        risk = sign * (entry - stop)
        rewards = [sign * (target - entry) for target in targets]
        if risk <= 0 or any(reward <= 0 for reward in rewards):
            raise ValueError("invalid_trade_plan_geometry")
        if any(b <= a for a, b in zip(rewards, rewards[1:])):
            raise ValueError("invalid_trade_plan_target_order")
        ratios = [reward / risk for reward in rewards]
        from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
        if any(rr < HARD_MIN_AUTO_PAPER_NET_RR for rr in ratios):
            raise ValueError("trade_plan_rr_below_two")
        output["trade_plan"].update(entry_price=entry, stop_loss=stop,
                                  take_profits=targets, risk_reward=ratios)
    return output


INSTRUCTIONS = """Use market.strategy_features when available as descriptive context only. EMA9/20 and RSI14 help compare momentum and trend; do not turn crosses or RSI extremes into mandatory entry conditions. Missing values are not directional evidence.
Use market.poi_context when present to discuss fresh/retested/invalidated candle-derived order blocks and support/resistance. These are context, not automatic entry triggers or evidence of institutional holdings.
Write a Persian multi-day BTC market outlook, horizon 3–7 days,
using only the supplied complete weekly, daily and 4h OHLCV, verified price levels,
and optional source-labelled market intelligence. This is a conditional report plan,
not an instruction to execute an order. Assess weekly context, daily structure and 4h development. Select a
dominant conditional scenario and a different alternative. Give explicit activation
and invalidation conditions (timeframe and close/retest), referenced level IDs,
and two short conditional stages of a plausible path. Provide path_level_ids with 2–6 known level IDs in the same traversal order, ending at a target_level_id. Retracements may repeat IDs. This sequence is a schematic scenario overlay, not predicted timestamps. Do not forecast exact daily prices,
dates of target arrival, guaranteed direction, confidence percentages or win rates.
Only use IDs from levels; never invent a price level. In prose, give a plain-language level description and its supplied price in dollars.
Copy prices from the matching level; never invent, estimate or substitute a price.
If evidence is weak prefer range/unclear descriptions and explain uncertainty.
No external news/calendar feed is connected: do not invent news, Fed probabilities,
events or catalysts. Historical liquidations are not future heatmap levels. Binance
OI is not Bitunix positioning; provisional trade delta is not verified CVD. For supplemental_evidence, include one short Persian explanation for EACH key actually
present in market.market_intelligence.sources, and no absent source. effect is supports,
opposes or neutral relative to the DOMINANT scenario (not simply bullish/bearish);
use not_assessed only if you cannot assess it. Explain the supplied observation and
its limitation; neutral is appropriate for inconclusive or short-lived observations.
Never force directional evidence from funding sign, tiny changes or a partial trade
bucket. Do not treat Binance sampled liquidations as complete market totals, or
Order Book imbalance as durable demand. Missing sources are unavailable, not zero.
These explanations describe your stated assessment, not a proven causal influence.
Optional
microdata is secondary context for this multi-day horizon, not a direction gate.
Optional channel_context describes the SAME major-swing channel drawn on the chart.
Use it only as secondary context alongside candles and horizontal levels. Mention its
position/boundaries briefly when material to a scenario. A quote near an edge is not
an entry signal, an automatic veto, or proof of reversal. Outside the channel is not
a confirmed breakout; retain close/retest requirements. historical=true means an
older fitted structure, whose projection may no longer be active. If unavailable,
do not infer a channel. Channel bounds move with time: supplied bounds apply only
at as_of, are not executable Entry/SL/TP levels, and must not be added to level IDs.
Channel direction must not override stronger contradictory price evidence.
If data_quality_warnings is nonempty, disclose the historical source anomaly in uncertainty_fa; do not treat the source as fully clean.
Previous report is for comparison only: explicitly say what changed or stayed stable.
Keep the ENTIRE narrative concise: aim for 250–350 Persian words including the plan.
Write for a Persian-speaking reader who understands basic trading but not specialist
jargon. Use natural, straightforward Persian, short sentences and one idea per sentence.
Do not compress several technical qualifiers into an obscure phrase just to be brief.
Lead with what the market is doing, then the price condition that would support a fall
or rise, and distinguish confirmed evidence from a condition still awaiting confirmation.
Preserve the evidence's uncertainty: a directional lean is not a measured probability.
Prefer "فعلاً نشانه‌های افت کمی بیشتر است، اما هنوز تأیید کافی نداریم" to
"فشار نزولی مشروط اندکی غالب است" when that is what the evidence supports.
Prefer "برگشت قیمت بالای این سطح" to "بازپس‌گیری", and
"این شرط هنوز تأیید نشده است" to "محرک فعال نیست" when applicable.
Describe "سقف ۱۲ کندل ۴ساعته" as "بالاترین قیمت در دو روز اخیرِ کامل‌شده";
describe the low similarly and the prior complete week's low as
"کمترین قیمت هفتهٔ کامل گذشته". Include the matching supplied price alongside each
key level in the summary and activation/invalidation conditions, rather than asking
the reader to infer prices from labels or chart chips. Never expose raw level IDs in prose.
When a candle close or retest is required, keep that exact timeframe and condition:
use "بسته‌شدن کندل ۴ساعته زیر این قیمت" or "برگشت دوباره به سطح و حفظ آن" as appropriate;
do not simplify a required close into a momentary price touch. Avoid unexplained ICT,
MSS, FVG, liquidity and order-flow jargon; explain the practical meaning briefly if needed.
Apply this readable style to every narrative field, including trade-plan reasons and
entry conditions. Keep the existing length limits, conditional Entry/SL/TP plan,
price grounding and uncertainty requirements.
Summary: 2–3 short sentences. Weekly/daily context: one sentence each. Each scenario:
one sentence of thesis, one activation and one invalidation condition, two short path
stages (at most 180 characters each). Watch list: at most two short items. Changes and
uncertainty: one sentence each; preserve material data-quality warnings. Do not repeat
the same explanation across fields or add educational introductions.
Describe support reactions and distinguish a confirmed break from a possible bounce.
Keep multi-day scenarios separate from execution. Use action=wait with null entry/stop
and empty targets in trade_plan. The server supplies the intraday execution card.
All narrative strings must be Persian. Return ONLY one JSON object matching this
schema (no extra fields):
""" + json.dumps(Outlook.model_json_schema(), ensure_ascii=False)


INDEPENDENT_INSTRUCTIONS = """Write an independent Persian BTCUSDT higher-timeframe market outlook.
Your primary evidence is the supplied CLOSED weekly and daily OHLCV and fresh live quote.
Use 4h only to explain the current development. Analyse the candles yourself; no trading
engine candidate, WAIT state, trend filter, POI list or channel should determine your conclusion.
The report updates daily; its conditional horizon is several days to roughly two weeks,
not a promise to reach a price tomorrow. State the available history's limitations:
Read actual market.history_coverage counts; even one year cannot establish a multi-year cycle top/bottom.
Explain weekly direction and daily structure separately. Distinguish a correction within
a larger trend, continuation, range, and possible reversal. Never claim a trend has ended
merely because price touches support or momentum slows. In trend_assessment explain:
phase_fa: where price is in the trend and the candle evidence;
reversal_watch_fa: where caution rises, why, and what daily/weekly close would confirm or refute reversal;
expected_path_fa: ordered conditional destinations with dollar prices and a competing path;
horizon_fa: approximate scenario horizon, explicitly not a target-arrival deadline.
Choose a dominant scenario and a different alternative, with clear activation and invalidation
conditions referencing actual levels. Each path_fa has exactly two concise conditional stages.
Separate retest_level_ids (pullback/retest areas) from target_level_ids (destinations).
For directional scenarios, targets must lie beyond both the live quote and the last
activation/path reference in the stated direction. Never put a retest above spot into
a bearish target just to fill a field (or the inverse for bullish).
When a grounded destination is unavailable, use empty target_level_ids,
path_level_ids and target_zone_level_ids, and explain briefly in target_unavailable_reason_fa.
You can still state the directional thesis and retest levels; this is NOT a WAIT gate.
Otherwise path_level_ids has 2-6 IDs ending at a target ID. No fabricated probabilities or win rates.
For each scenario, target_zone_level_ids may contain exactly two grounded level IDs,
lower then upper, bounding the FINAL path destination. Use a narrow structurally justified
reaction area, not the entire distance between successive targets. Explain the area in the
scenario thesis. If two meaningful bounds are unavailable, use [] and a precise target
level; never invent a width for display. The plotted path is schematic, not arrival timing.
Existing market.levels are reference values, not an exhaustive list or directional verdict.
You may identify up to eight additional important levels yourself in analyst_levels, each
copied EXACTLY from a supplied daily/weekly candle high, low or close. Supply the candle's
open_time_ms, timeframe, field, matching price, unique ai_ prefixed ID, Persian label and
reason based on visible reactions/structure. Never manufacture an unseen price or cite an
unclosed candle. Scenario IDs may reference existing or your grounded analyst_levels.
A cited historical price alone does not prove support strength: explain why it matters.
All narrative prices must match those cited levels. Distinguish a possible bounce from
confirmed reversal and discuss what would change your assessment.
Optional source-labelled market intelligence is secondary, not an entry gate. For each
available market.market_intelligence.sources key provide supplemental_evidence explaining
support/opposition/neutrality relative to the dominant scenario and the source limitations.
Do not invent absent sources, news, web searches, catalysts or economic releases. There is
no web research feed in this mode; your learned knowledge explains methods, not live facts.
Do not equate sampled liquidation/order-book data with complete or durable market positioning.
Mention supplied data_quality_warnings and material missing data in uncertainty_fa.
Compare with previous_report: say what changed, persisted, or was invalidated; do not claim
verified forecasting accuracy from a previous narrative alone.
Optionally provide swing_plan when a convincing multi-day opportunity exists; otherwise
use null or action=wait. It is advisory and must NEVER open an order. Use one simple 4h
or daily confirmation, not multiple mandatory ICT gates or 5m triggers. Holding horizon
is 2-14 days, typically 7; entry_valid_hours is separate, at most 48 hours. Do not force
one trade per report. Entry, stop and target IDs must be grounded levels. Explain structural
invalidation and the first significant obstacle. Minimum stop distance is 0.5 daily ATR;
this is only a noise floor, never a reason to fabricate/widen a stop. Estimated NET R:R
must be >=1.5 after supplied fees, spread/slippage; future funding is unknown and excluded.
If structure does not permit those constraints, omit the swing proposal instead of forcing it.
market.previous_outlooks contains dated past reports and their saved levels/plans. Review the
most relevant prior outlook (even if it had no trade) with previous_plan_review.prior_report_id, explaining
whether its thesis still holds and its next conditional destination. Say 'اگر وارد شده‌اید':
actual fills/positions are NOT tracked here. Do not invent a fill, profit, SL or TP hit.
Late entry is a NEW assessment at today's price, not reuse of an expired entry or claimed
trade extension. State whether chasing is unsuitable or a fresh conditional entry is warranted.
market.previous_outlooks[].price_progress lists observed ranges and level touches from
fully closed 4h bars starting AFTER that report. These are price observations only:
a target touch does not prove scenario activation, a fill, trade success, or execution
order within a candle. first_touch_open_ms is the bar opening time, not exact touch time.
Insufficient coverage means unknown; do not claim the full path was observed.
Never silently revise the old entry/stop/targets; any new swing_plan is a separate proposal.
Entry expiry is not a position holding deadline. No previous outlook means review=null; when history exists, include a review of its scenario.
The legacy trade_plan must use action=wait,
null entry_level_id and stop_level_id, empty target_level_ids, and explain that entry
requires a separate fresh assessment. No execution engine snapshot is attached.
Use readable Persian throughout, explicit dollar levels, and no raw IDs in prose.
Keep all narrative text together about 200-300 Persian words. Summary: at most 60 words.
Each remaining field should be one short sentence; avoid repeating the summary.
State the current directional assessment first, then a simple condition that changes it.
Do not require close + reclaim + retest simultaneously just to express an outlook.
Keep uncertainty_fa and change_since_previous_fa to one concise sentence each;
the schema character limits are safety ceilings, not targets for verbosity. Return only
one JSON object matching the schema. trend_assessment is REQUIRED in this mode.
""" + json.dumps(Outlook.model_json_schema(), ensure_ascii=False)



def validate_swing_plan(plan: SwingPlan | None, levels: list[dict], context: dict) -> dict | None:
    """Advisory only: no order/position writes, no intraday target cap."""
    if plan is None:
        return None
    from datetime import datetime, timedelta
    output = {**plan.model_dump(), "source": "independent_swing", "execution_status": "not_tracked"}
    if plan.action == "wait":
        return output
    prices = {level["id"]: float(level["price"]) for level in levels}
    ids = [plan.entry_level_id, plan.stop_level_id, *plan.target_level_ids]
    if not set(ids) <= set(prices):
        raise ValueError("unknown_swing_level")
    entry, stop, *targets = [prices[key] for key in ids]
    sign = 1 if plan.action == "long" else -1
    risk = sign * (entry-stop)
    rewards = [sign*(target-entry) for target in targets]
    atr = context.get("daily_atr14")
    quote = context.get("live_price")
    if (not all(math.isfinite(p) and p > 0 for p in [entry, stop, *targets])
            or risk <= 0 or any(r <= 0 for r in rewards)
            or any(b <= a for a,b in zip(rewards, rewards[1:]))):
        raise ValueError("invalid_swing_geometry")
    # A floor rejects narrow plans; it never invents or moves a structural stop.
    if not isinstance(atr, (int,float)) or not math.isfinite(atr) or atr <= 0 or risk < 0.5*atr:
        raise ValueError("swing_stop_too_tight")
    if not isinstance(quote,(int,float)) or not math.isfinite(quote) or sign*(quote-stop) <= 0 or sign*(targets[0]-quote) <= 0:
        raise ValueError("swing_already_invalidated")
    from app.services.execution_model import ExecutionModel
    from app.config import get_effective_settings
    cfg = get_effective_settings().model_copy(update=context.get("swing_cost_assumptions", {}))
    model = ExecutionModel(cfg)
    rr = [model.calc_rr(plan.action.upper(), entry, stop, target, "STOP", min_net_rr=1.5)
          for target in targets]
    if any(not r.acceptable or not math.isfinite(r.net_rr) for r in rr):
        raise ValueError("swing_net_rr_below_minimum")
    stamp = datetime.fromisoformat(context["as_of"].replace("Z", "+00:00"))
    output.update(entry_price=entry, stop_loss=stop, take_profits=targets,
        risk_reward=[r.net_rr for r in rr],
        entry_expires_at=(stamp+timedelta(hours=plan.entry_valid_hours)).isoformat(),
        cost_assumptions=context.get("swing_cost_assumptions", {}),
        funding_included=False)
    return output
