from __future__ import annotations

import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scenario(StrictModel):
    title_fa: str = Field(min_length=3, max_length=180)
    direction: Literal["bullish", "bearish", "range"]
    thesis_fa: str = Field(min_length=20, max_length=1800)
    activation_fa: str = Field(min_length=10, max_length=800)
    invalidation_fa: str = Field(min_length=10, max_length=800)
    activation_level_ids: list[str] = Field(min_length=1, max_length=3)
    invalidation_level_ids: list[str] = Field(min_length=1, max_length=3)
    target_level_ids: list[str] = Field(min_length=1, max_length=3)
    path_fa: list[str] = Field(min_length=2, max_length=4)


class Outlook(StrictModel):
    summary_fa: str = Field(min_length=30, max_length=2400)
    weekly_context_fa: str = Field(min_length=10, max_length=1200)
    daily_structure_fa: str = Field(min_length=10, max_length=1200)
    dominant: Scenario
    alternative: Scenario
    uncertainty_fa: str = Field(min_length=10, max_length=1200)
    watch_next_fa: list[str] = Field(min_length=1, max_length=5)
    change_since_previous_fa: str = Field(min_length=5, max_length=1000)

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
        ids = scenario.activation_level_ids + scenario.invalidation_level_ids + scenario.target_level_ids
        if not set(ids) <= allowed:
            raise ValueError("unknown_evidence_level")
        if any(not stage.strip() or len(stage) > 800 for stage in scenario.path_fa):
            raise ValueError("invalid_scenario_stage")
    return result.model_dump()


INSTRUCTIONS = """Write a Persian multi-day BTC market outlook, horizon 3–7 days,
using only the supplied complete weekly, daily and 4h OHLCV, verified price levels,
and optional source-labelled market intelligence. This is market context, not an
entry signal. Assess weekly context, daily structure and 4h development. Select a
dominant conditional scenario and a different alternative. Give explicit activation
and invalidation conditions (timeframe and close/retest), referenced level IDs,
and 2–4 conditional stages of a plausible path. Do not forecast exact daily prices,
dates of target arrival, guaranteed direction, confidence percentages or win rates.
Only use IDs from levels; never invent a price level. Refer to level labels in prose.
If evidence is weak prefer range/unclear descriptions and explain uncertainty.
No external news/calendar feed is connected: do not invent news, Fed probabilities,
events or catalysts. Historical liquidations are not future heatmap levels. Binance
OI is not Bitunix positioning; provisional trade delta is not verified CVD. Optional
microdata is secondary context for this multi-day horizon, not a direction gate.
If data_quality_warnings is nonempty, disclose the historical source anomaly in uncertainty_fa; do not treat the source as fully clean.
Previous report is for comparison only: explicitly say what changed or stayed stable.
All narrative strings must be Persian. Return ONLY one JSON object matching this
schema (no extra fields):
""" + json.dumps(Outlook.model_json_schema(), ensure_ascii=False)
