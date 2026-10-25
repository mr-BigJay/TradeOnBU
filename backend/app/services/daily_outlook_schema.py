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
    target_level_ids: list[str] = Field(min_length=1, max_length=3)
    path_fa: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(min_length=2, max_length=2)
    path_level_ids: list[str] = Field(default_factory=list, max_length=6)


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


class Outlook(StrictModel):
    trade_plan: TradePlan
    summary_fa: str = Field(min_length=30, max_length=550)
    weekly_context_fa: str = Field(min_length=10, max_length=250)
    daily_structure_fa: str = Field(min_length=10, max_length=250)
    dominant: Scenario
    alternative: Scenario
    uncertainty_fa: str = Field(min_length=10, max_length=250)
    watch_next_fa: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(min_length=1, max_length=2)
    change_since_previous_fa: str = Field(min_length=5, max_length=200)

    @model_validator(mode="after")
    def different_scenarios(self):
        if self.dominant.direction == self.alternative.direction:
            raise ValueError("alternative_must_have_a_different_direction")
        return self


def parse_outlook(text: str, context: dict) -> dict:
    raw = text.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    result = Outlook.model_validate(json.loads(raw))
    allowed = {level["id"] for level in context["levels"]}
    for scenario in (result.dominant, result.alternative):
        ids = scenario.activation_level_ids + scenario.invalidation_level_ids + scenario.target_level_ids + scenario.path_level_ids
        if scenario.path_level_ids and (len(scenario.path_level_ids) < 2 or scenario.path_level_ids[-1] not in scenario.target_level_ids):
            raise ValueError("invalid_chart_path")
        if not set(ids) <= allowed:
            raise ValueError("unknown_evidence_level")
        if any(not stage.strip() or len(stage) > 180 for stage in scenario.path_fa):
            raise ValueError("invalid_scenario_stage")
    output = result.model_dump()
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
        if any(rr < 2 for rr in ratios):
            raise ValueError("trade_plan_rr_below_two")
        output["trade_plan"].update(entry_price=entry, stop_loss=stop,
                                  take_profits=targets, risk_reward=ratios)
    return output


INSTRUCTIONS = """Write a Persian multi-day BTC market outlook, horizon 3–7 days,
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
OI is not Bitunix positioning; provisional trade delta is not verified CVD. Optional
microdata is secondary context for this multi-day horizon, not a direction gate.
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
Finish with trade_plan: one conditional long/short idea with a precise confirmation
(timeframe, close/retest) and an entry, stop and one or two ordered targets using ONLY
supplied level IDs. Explain why these levels fit the structure; never move a stop or
choose an arbitrary distant target just to achieve a ratio. Every target must offer
reward/risk >= 2 from the stated entry; long requires stop < entry < targets, short
requires targets < entry < stop. This is price-only R:R before fees/slippage. Do not
claim the trigger is currently met. If no defensible plan exists, use action=wait,
null entry/stop and empty targets, explain why and what confirmation to wait for.
All narrative strings must be Persian. Return ONLY one JSON object matching this
schema (no extra fields):
""" + json.dumps(Outlook.model_json_schema(), ensure_ascii=False)
