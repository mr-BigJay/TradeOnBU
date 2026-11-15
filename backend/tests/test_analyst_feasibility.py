"""Executable geometry and trend pullbacks, not promises of profitable fills."""
import json
from copy import deepcopy

import pytest

from app.domain.candles import Candle
from app.services import multi_setup_analyst as a
from app.services.analyst_feasibility import build_execution_feasibility
from app.services.analyst_payload import build_analyst_payload
from app.services.analyst_regime import continuation_direction, executable_regime
from app.services.entry_strategies import validate_strategy_plan
from app.services.ict_confirmation_engine import _token_on_bar
from tests.test_multi_setup_analyst import fixture, proposal


@pytest.mark.parametrize("higher,hourly,expected", [
    ("bearish", "bullish", "SHORT"), ("bullish", "bearish", "LONG"),
    ("bearish", "range", "SHORT"), ("bullish", "unclear", "LONG"),
    ("range", "bullish", "LONG"), ("unclear", "bearish", "SHORT"),
    ("range", "range", None),
])
def test_higher_trend_accepts_corrective_hourly_structure(higher, hourly, expected):
    assert continuation_direction({"4h": {"trend": higher}, "1h": {"trend": hourly}}) == expected


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_pullback_passes_compiler_and_downstream_strategy_guard(direction):
    s, c, e, n = fixture()
    # 1H correction against rising 4H trend.
    s["1h"] = [Candle(2100-b.open, 2100-b.low, 2100-b.high, 2100-b.close, b.time_ms)
               for b in s["1h"]]
    p = proposal("trend_continuation")
    if direction == "SHORT":
        s = {tf: [Candle(2200-b.open, 2200-b.low, 2200-b.high, 2200-b.close, b.time_ms)
                  for b in bars] for tf, bars in s.items()}
        for item in e:
            item.update(low=2200-item["high"], high=2200-item["low"], price=2200-item["price"],
                        kind={"support":"resistance", "resistance":"support"}[item["kind"]])
        p["direction"] = "SHORT"
    frames = executable_regime(s)["timeframes"]
    assert frames["1h"]["trend"] != frames["4h"]["trend"]
    plan = a.compile_plan(p, e, s, c, n, n)
    assert validate_strategy_plan(plan) is None
    assert plan["trade_against_dominant_trend"] is False
    assert plan["verified_trade_plan"]["confirmation_type"] == "htf_pullback_rejection"
    assert plan["verified_trade_plan"]["seed_confirmations"] == {}
    # Merely touching an area with a flat candle is still not proof.
    assert not _token_on_bar("htf_pullback_rejection", direction=direction,
        window=[Candle(plan["entry_price"], plan["entry_zone_high"], plan["entry_zone_low"],
                       plan["entry_price"], n-300000)],
        zone_lo=plan["entry_zone_low"], zone_hi=plan["entry_zone_high"], structured=plan)


def test_diagnostic_limit_matches_execution_when_hourly_atr_is_larger():
    s, c, e, n = fixture()
    s["1h"] = [Candle(b.open, b.high+30, b.low-30, b.close, b.time_ms) for b in s["1h"]]
    e[1].update(low=990, high=992, price=991)
    e[-1].update(low=1150, high=1152, price=1151)
    p = proposal("ict_reversal")
    plan = a.compile_plan(p, e, s, c, n, n)
    diag = a._proposal_diagnostic(p, e, s, 1, c)
    assert diag["stop_distance_limit"] > 3*diag["atr15m"]
    assert diag["stop_distance_limit"] == plan["verified_trade_plan"]["evidence"]["stop_distance_limit"]
    assert diag["stop"] == plan["stop_loss"]
    assert diag["target"] == plan["take_profit"]


def test_insufficient_reward_reports_actual_obstacle_and_cost_adjusted_rr():
    s, c, e, n = fixture()
    e.append(dict(e[-1], id="near", low=1024, high=1026, price=1025))
    result = a.compile_response({"plans":[proposal("ict_reversal")]}, e,s,c,n,n,
                                {sid:True for sid in a.PROFILES})
    diag = result["analyst_summary"]["rejected_plans"][0]
    assert diag["reason"] == "insufficient_net_reward_before_obstacle"
    assert diag["obstacle_level_id"] == "near"
    assert diag["obstacle_net_rr"] < diag["required_min_net_rr"] == 1.5
    assert diag["target"] is None


def test_preflight_choices_are_recompilable_and_do_not_mutate_inputs():
    s, c, e, n = fixture()
    original = deepcopy((s,e))
    enabled = {sid:True for sid in a.PROFILES}
    hints = build_execution_feasibility(e,s,c,n,enabled)
    assert {x["strategy_id"] for x in hints["choices"]} == set(a.PROFILES)
    for item in hints["choices"]:
        plan = a.compile_plan(dict(item, reason_fa="انتخاب مستقل تحلیل‌گر"), e,s,c,n,n)
        assert plan["entry_price"] == item["entry"]
        assert plan["take_profit"] == item["target"]
    assert (s,e) == original
    text, audit = build_analyst_payload(market={"timeframes":{}}, evidence_catalog=e,
        market_intelligence={}, strategy_features={}, enabled_strategies=enabled,
        execution_regime=executable_regime(s), execution_feasibility=hints)
    assert json.loads(text)["execution_feasibility"] == hints
    assert audit["sections"]["execution_feasibility"]["after_chars"] > 0
    # Hints do not become a mandatory shortlist.
    e.append(dict(e[-1], id="farther_target", price=1100, low=1099, high=1101))
    independent = dict(proposal("ict_reversal"), target_id="farther_target")
    assert a.compile_plan(independent,e,s,c,n,n)


def test_disabled_strategy_has_no_hint_and_empty_geometry_is_not_fabricated():
    s,c,e,n = fixture()
    assert build_execution_feasibility(e,s,c,n,{})["choices"] == []
    e[-1]["price"] = 1023
    hints = build_execution_feasibility(e,s,c,n,{"ict_reversal":True})
    assert hints["choices"] == []
    assert hints["rejection_counts"]
