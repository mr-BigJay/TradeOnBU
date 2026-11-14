import json
import pytest
from app.services.daily_outlook_schema import parse_outlook
from tests.test_daily_outlook import report
from tests.test_independent_outlook import market, proposal


def prepared():
    ctx = market()
    ctx["swing_entry_policy"] = "conditional_entry_v2"
    ctx["levels"].append({"id": "obstacle", "price": 118})
    data = report()
    data["swing_plan"] = proposal().model_dump()
    data["swing_plan"].update(
        execution={"method": "limit_after_close", "timeframe": "4h",
                   "close_side": "above", "confirmation_level_id": "entry"},
        first_obstacle={"assessment": "identified", "level_id": "obstacle",
                        "reason_fa": "مقاومت روزانه با چند واکنش مشخص در مسیر ورود تا هدف."})
    return ctx, data


def test_valid_plan_has_one_close_then_future_limit_without_claimed_fill():
    ctx, data = prepared()
    parsed = parse_outlook(json.dumps(data), ctx)
    plan = parsed["swing_plan"]
    assert plan["action"] == "long"
    assert plan["entry_price"] == 100 and plan["stop_loss"] == 90
    assert plan["take_profits"] == [120] and plan["obstacle_net_rr"] > 1.5
    assert plan["order_type"] == "LIMIT"
    assert plan["confirmation_status"] == "not_evaluated"
    assert plan["execution_status"] == "not_tracked"
    assert "بدون نیاز به بسته‌شدن دوم" in plan["trigger_fa"]
    assert plan["original_trigger_fa"] == data["swing_plan"]["trigger_fa"]


def test_saved_btc_geometry_rejects_near_obstacle_not_distant_target():
    ctx, data = prepared()
    ctx.update(live_price=65500, daily_atr14=2500)
    ctx["levels"] += [
        {"id": "btc_entry", "price": 65039.6}, {"id": "btc_stop", "price": 67455.5},
        {"id": "btc_target", "price": 59846.7}, {"id": "btc_obstacle", "price": 62499.9}]
    plan = data["swing_plan"]
    plan.update(action="short", entry_level_id="btc_entry", stop_level_id="btc_stop",
                target_level_ids=["btc_target"])
    plan["execution"].update(close_side="below", confirmation_level_id="btc_entry")
    plan["first_obstacle"]["level_id"] = "btc_obstacle"
    result = parse_outlook(json.dumps(data), ctx)
    rejected = result["swing_plan"]
    assert rejected["action"] == "wait"
    assert rejected["validation_reason"] == "swing_obstacle_rr_below_minimum"
    assert rejected["validation_details"]["obstacle_net_rr"] < 1.5
    assert rejected["rejected_proposal"]["entry_level_id"] == "btc_entry"
    assert result["summary_fa"] == data["summary_fa"]


@pytest.mark.parametrize("change,reason", [
    ("missing", "swing_execution_missing"),
    ("wrong_side", "swing_execution_geometry"),
    ("unknown_confirmation", "swing_execution_geometry"),
    ("late_confirmation", "swing_execution_geometry"),
    ("unknown_obstacle", "swing_obstacle_invalid"),
    ("wrong_obstacle_side", "swing_obstacle_invalid"),
    ("beyond_target", "swing_obstacle_invalid"),
])
def test_invalid_entry_metadata_rejects_only_plan(change, reason):
    ctx, data = prepared()
    plan = data["swing_plan"]
    if change == "missing": plan["execution"] = None
    if change == "wrong_side": plan["execution"]["close_side"] = "below"
    if change == "unknown_confirmation": plan["execution"]["confirmation_level_id"] = "missing"
    if change == "late_confirmation": plan["execution"]["confirmation_level_id"] = "target"
    if change == "unknown_obstacle": plan["first_obstacle"]["level_id"] = "missing"
    if change == "wrong_obstacle_side": plan["first_obstacle"]["level_id"] = "low"
    if change == "beyond_target":
        ctx["levels"].append({"id": "beyond", "price": 130})
        plan["first_obstacle"]["level_id"] = "beyond"
    result = parse_outlook(json.dumps(data), ctx)
    assert result["swing_plan"]["validation_reason"] == reason
    assert result["summary_fa"] == data["summary_fa"]


def test_no_significant_obstacle_is_allowed_without_gating_every_nearby_level():
    ctx, data = prepared()
    data["swing_plan"]["first_obstacle"].update(
        assessment="none_in_path", level_id=None,
        reason_fa="خطوط نزدیک واکنش ساختاری مشخص ندارند؛ نخستین مقاومت مهم خود هدف است.")
    plan = parse_outlook(json.dumps(data), ctx)["swing_plan"]
    assert plan["action"] == "long" and plan["obstacle_price"] is None


def test_legacy_plan_is_readable_but_not_marked_as_new_policy_validated():
    ctx = market()
    data = report()
    data["swing_plan"] = proposal().model_dump()
    plan = parse_outlook(json.dumps(data), ctx)["swing_plan"]
    assert plan["action"] == "long" and plan["entry_price"] == 100
    assert plan["entry_policy_status"] == "legacy_unassessed"
    ctx["swing_entry_policy"] = "conditional_entry_v2"
    assert parse_outlook(json.dumps(data), ctx)["swing_plan"]["validation_reason"] == "swing_execution_missing"


def test_valid_short_with_better_structural_entry_passes_without_moving_levels():
    ctx, data = prepared()
    ctx["live_price"] = 98
    ctx["levels"] = [
        {"id": "entry", "price": 100}, {"id": "low", "price": 110},
        {"id": "high", "price": 110}, {"id": "target", "price": 80},
        {"id": "obstacle", "price": 82}]
    data["swing_plan"]["action"] = "short"
    data["swing_plan"]["execution"]["close_side"] = "below"
    plan = parse_outlook(json.dumps(data), ctx)["swing_plan"]
    assert plan["action"] == "short"
    assert plan["entry_price"] == 100 and plan["stop_loss"] == 110
    assert plan["obstacle_net_rr"] > 1.5


def test_telegram_explains_obstacle_rejection():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.services.outlook_telegram import format_outlook_message
    ctx, data = prepared()
    ctx["levels"][-1]["price"] = 110
    parsed = parse_outlook(json.dumps(data), ctx)
    row = SimpleNamespace(report=parsed, context=ctx, symbol="BTCUSDT", id=8, slot_close_ms=0)
    text = format_outlook_message(row, datetime.now(timezone.utc), "")
    assert "مانع بررسی‌شده" in text
    assert "R:R خالص تا مانع" in text
    assert "برنامهٔ ورود مشروط: لانگ" not in text
