"""Major 4h/1h level selection is evidence-based and separate from Paper entry."""
import math

from app.domain.candles import Candle
from app.services.analyst_major_levels import select_major_levels, minimum_forecast_gap
from app.services.analyst_outlook import compile_forecasts, format_major_levels_fa
from app.services import multi_setup_analyst as analyst
from tests.test_multi_setup_analyst import fixture


def _cyclic_series():
    series, costs, evidence, now = fixture()
    for tf in ("1h", "4h"):
        step = analyst.STEPS[tf]
        boundary = now // step * step
        prices = [1000 + 60 * math.sin(i * math.pi / 8) for i in range(96)]
        series[tf] = [
            Candle(p, p + 2, p - 2, p, boundary - (96 - i) * step)
            for i, p in enumerate(prices)
        ]
    return series, now


def test_major_levels_require_real_unbroken_confirmed_htf_structure():
    series, now = _cyclic_series()
    catalog = analyst.catalog(series, now)
    major = select_major_levels(catalog, series)
    assert major, "Clear repeated 4h swing reactions must produce real major levels."
    assert {"support", "resistance"} <= {x["role"] for x in major}
    assert all(x["timeframe"] in ("1h", "4h") for x in major)
    assert all(x["reaction_atr4h"] >= 0.70 for x in major)
    assert all(x["quality_score"] > 0 and x["confluence_count"] >= 0 for x in major)
    assert all(x["kind"] in ("support", "resistance", "OB") for x in major)
    assert len(major) <= 8
    # The original zone ID and source bounds must be preserved; no synthetic levels.
    source_by_id = {x["id"]: x for x in catalog}
    assert all(x["id"] in source_by_id for x in major)
    assert all((x["low"], x["high"]) == (
        source_by_id[x["id"]]["low"], source_by_id[x["id"]]["high"]
    ) for x in major)
    assert not any(x["timeframe"] in ("5m", "15m") for x in major)
    assert "حمایت" in format_major_levels_fa(major)
    assert "مقاومت" in format_major_levels_fa(major)


def test_fake_htf_level_and_ltf_level_cannot_become_major():
    series, now = _cyclic_series()
    catalog = analyst.catalog(series, now)
    fake = {
        "id": "invented_level", "kind": "support", "timeframe": "4h",
        "time_ms": series["4h"][8].time_ms, "confirmed_ms": series["4h"][11].time_ms,
        "low": 900, "high": 902, "price": 901, "status": "fresh", "touch_count": 0,
    }
    small = dict(fake, id="micro_level", timeframe="5m")
    major = select_major_levels(catalog + [fake, small], series)
    assert "invented_level" not in {x["id"] for x in major}
    assert "micro_level" not in {x["id"] for x in major}


def test_empty_evidence_never_fabricates_levels():
    series, now = _cyclic_series()
    assert select_major_levels([], series) == []
    assert "سطح 5m/15m جایگزین نمی‌شود" in format_major_levels_fa([])
    assert minimum_forecast_gap(series) > 0


def _outlook_evidence():
    def z(id_, kind, tf, low, high):
        return {"id": id_, "kind": kind, "timeframe": tf, "low": low,
                "high": high, "price": (low + high) / 2, "direction": None}
    return [
        z("support", "support", "4h", 100, 110),
        z("micro", "support", "15m", 100, 110),
        z("overlap", "resistance", "4h", 105, 130),
        z("close", "resistance", "1h", 140, 155),
        z("middle", "resistance", "4h", 260, 280),
        z("far", "resistance", "4h", 500, 520),
    ]


def _raw(level_id, target_id):
    return [{"scenario_type": "support_bounce", "direction": "LONG",
             "level_id": level_id, "target_id": target_id,
             "reason_fa": "سطح مستند است؛ ورود تنها پس از تأیید ساختار"}]


def _screen(proposals, eligible, gap=100):
    evidence = _outlook_evidence()
    major = [dict(id=i, quality_score=6, role="support" if i == "support" else "resistance",
                  proof="confirmed_unbroken_swing", reaction_atr4h=1,
                  confluence_count=1) for i in eligible]
    return compile_forecasts(proposals, evidence, major_levels=major, min_gap=gap)


def test_forecasts_reject_micro_unproven_overlapping_near_target_and_intermediate_obstacle():
    cases = [
        ("micro", "far", ["support", "far"], "not_proven_major_htf_level"),
        ("support", "micro", ["support", "far"], "target_not_proven_major_htf_level"),
        ("support", "overlap", ["support", "overlap"], "target_overlaps_or_behind_source"),
        ("support", "close", ["support", "close"], "target_without_htf_room"),
        ("support", "far", ["support", "middle", "far"], "nearer_major_obstacle"),
    ]
    for src, target, allowed, expected in cases:
        items, rejected = _screen(_raw(src, target), allowed)
        assert not items
        assert rejected == [f"1:{expected}"]


def test_one_valid_major_scenario_and_no_forced_scenarios():
    accepted, rejected = _screen(_raw("support", "far"), ["support", "far"])
    assert not rejected
    assert len(accepted) == 1
    assert accepted[0]["watchlist_only"] and not accepted[0]["paper_order"]
    assert accepted[0]["level"]["major_evidence"]["reaction_atr4h"] == 1
    empty, rejected = _screen(_raw("support", "far"), [])
    assert empty == []
    assert rejected == ["1:not_proven_major_htf_level"]
