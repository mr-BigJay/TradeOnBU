"""Regression checks for the 1158 analyst disagreement (paper-only)."""
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.services import multi_setup_analyst as analyst
from app.services import verified_trade_plan as verified
from app.services.analyst_payload import build_analyst_payload
from app.services.analyst_regime import executable_regime
from app.services.ai_telegram_summary import build_ai_telegram_summary
from tests.test_multi_setup_analyst import fixture, proposal


def test_execution_regime_is_identical_to_compiler_trend():
    series, costs, evidence, now = fixture()
    context = executable_regime(series)
    assert context["timeframes"]["1h"]["trend"] == verified.trend(series["1h"])
    assert context["timeframes"]["4h"]["trend"] == verified.trend(series["4h"])
    assert context["method"] == "verified_trade_plan.trend/swing-high-low/v1"
    assert len(context["timeframes"]["1h"]["last_two_highs"]) == 2
    assert len(context["timeframes"]["1h"]["last_two_lows"]) == 2
    assert analyst.compile_plan(proposal("trend_continuation"), evidence, series, costs, now, now)


def test_rejected_continuation_carries_swing_regimes_and_selected_ids():
    series, costs, evidence, now = fixture()
    from app.domain.candles import Candle
    series["1h"] = [Candle(1000, 1002, 998, 1000, b.time_ms)
                     for b in series["1h"]]
    p = proposal("trend_continuation")
    result = analyst.compile_response(
        {"market_summary_fa": "EMA را صعودی می‌دانم", "plans": [p]},
        evidence, series, costs, now, now, {sid: True for sid in analyst.PROFILES},
    )
    assert result["analyst_summary"]["accepted"] == 0
    assert result.get("strategy_id") is None
    rej = result["analyst_summary"]["rejected_plans"][0]
    assert rej["reason"] == "continuation_trend_conflict"
    assert rej["rank"] == 1
    assert (rej["zone_id"], rej["invalidation_id"], rej["target_id"]) == ("zone", "stop", "target")
    assert rej["zone_evidence"]["price"] == 1021
    assert rej["entry"] == 1021
    assert rej["stop"] < rej["entry"]
    assert rej["regime_1h"] == "range"
    assert result["analyst_summary"]["execution_regime"]["timeframes"]["1h"]["trend"] == "range"


def test_rejected_too_wide_stop_has_reproducible_entry_stop_and_atr():
    series, costs, evidence, now = fixture()
    evidence = deepcopy(evidence)
    evidence[1].update(low=900., high=902., price=901.)
    p = proposal("ict_reversal")
    result = analyst.compile_response(
        {"plans": [p]}, evidence, series, costs, now, now,
        {sid: True for sid in analyst.PROFILES},
    )
    assert result["analyst_summary"]["rejections"] == ["stop_too_wide"]
    rej = result["analyst_summary"]["rejected_plans"][0]
    assert rej["entry"] == 1021.
    assert rej["stop"] < rej["entry"]
    assert rej["stop_distance"] > rej["stop_distance_limit"]
    assert rej["atr5m"] == pytest.approx(verified.atr(series["5m"]))
    assert rej["atr15m"] == pytest.approx(verified.atr(series["15m"]))
    assert rej["invalidation_evidence"]["id"] == "stop"
    assert rej["invalidation_evidence"]["low"] == 900.


def test_candle_payload_lossless_and_section_size_audited():
    series, _, evidence, now = fixture()
    market = {"symbol": "BTCUSDT", "timeframes": {}}
    for tf, candles in series.items():
        market["timeframes"][tf] = {
            "interval": tf, "data_complete": True,
            "closed_count": len(candles),
            "candles": [{"time_ms": c.time_ms, "time_iso": datetime.fromtimestamp(
                c.time_ms/1000, timezone.utc).isoformat(), "open": c.open,
                "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                for c in candles],
        }
    source = deepcopy(market)
    text, audit = build_analyst_payload(
        market=market, evidence_catalog=evidence,
        market_intelligence={"funding": {"available": False}},
        strategy_features={"timeframes": {"4h": {"atr14": 1.0}}},
        enabled_strategies={"ict_reversal": True},
        execution_regime=executable_regime(series),
    )
    loaded = json.loads(text)
    assert market == source, "Input must remain untouched for charts and monitor"
    assert audit["saved_chars"] > 0
    assert audit["after_chars"] < audit["before_chars"]
    assert audit["sections"]["market"]["after_chars"] < audit["sections"]["market"]["before_chars"]
    for tf, frame in loaded["market"]["timeframes"].items():
        assert len(frame["candles"]) == len(source["timeframes"][tf]["candles"])
        assert frame["candles_columns"] == ["time_ms", "open", "high", "low", "close", "volume"]
        assert [
            {key: row[i] for i, key in enumerate(frame["candles_columns"])}
            for row in frame["candles"]
        ] == [
            {key: c[key] for key in frame["candles_columns"]}
            for c in source["timeframes"][tf]["candles"]
        ]
    assert loaded["evidence_catalog"] == evidence


def test_no_analyst_acceptance_has_neutral_telegram_label():
    series, costs, evidence, now = fixture()
    raw = {"plans": [dict(proposal("ict_reversal"), zone_id="missing")]}
    result = analyst.compile_response(raw, evidence, series, costs, now, now,
                                      {sid: True for sid in analyst.PROFILES})
    caption = build_ai_telegram_summary(
        symbol="BTCUSDT", live=1021., decision="NO_TRADE",
        structured=result, validation={"ok": True},
        report_fa="", analyzed_at=datetime.now(timezone.utc),
    )
    assert "بدون استراتژی پذیرفته‌شده" in caption
    assert "برگشت قیمت" not in caption
    assert result["analyst_summary"]["rejected_plans"][0]["reason"] == "unknown_evidence_id"
