import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outlook_calendar import proposals_allowed
from app.services.daily_outlook_schema import parse_outlook, INDEPENDENT_INSTRUCTIONS
from app.services.outlook_telegram import format_outlook_message
from tests.test_independent_outlook import market, proposal
from tests.test_daily_outlook import report


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-11-13T20:29:59+00:00", True),
    ("2026-11-13T20:30:00+00:00", False),
    ("2026-11-14T09:00:00+03:30", False),
    ("2026-11-14T20:29:59+00:00", False),
    ("2026-11-14T20:30:00+00:00", True),
])
def test_tehran_boundaries(stamp, allowed):
    assert proposals_allowed({"as_of": stamp}) is allowed


@pytest.mark.parametrize("submitted", [None, {"action": "short", "entry_price": 65000}, "invalid"])
def test_weekend_keeps_analysis_but_suppresses_any_entry(submitted):
    ctx, data = market(), report()
    ctx.update(as_of="2026-11-14T08:00:00+00:00", new_position_proposals_allowed=True)
    data["swing_plan"] = submitted
    parsed = parse_outlook(json.dumps(data), ctx)
    assert parsed["summary_fa"] == data["summary_fa"]
    assert parsed["dominant"]["direction"] == data["dominant"]["direction"]
    plan = parsed["swing_plan"]
    assert plan["action"] == "wait"
    assert plan["validation_reason"] == "weekend_analysis_only"
    assert plan["entry_level_id"] is None and plan["target_level_ids"] == []
    assert "entry_price" not in plan and "rejected_proposal" not in plan
    row = SimpleNamespace(report=parsed, context=ctx, symbol="BTCUSDT", id=1, slot_close_ms=0)
    message = format_outlook_message(row, datetime.now(timezone.utc), "")
    assert "شنبه به وقت تهران" in message
    assert "برنامهٔ ورود مشروط: شورت" not in message


def test_weekday_plan_still_valid_and_prompt_contains_analysis_only_override():
    ctx, data = market(), report()
    ctx["as_of"] = "2026-11-15T08:00:00+00:00"  # Sunday
    data["swing_plan"] = proposal().model_dump()
    assert parse_outlook(json.dumps(data), ctx)["swing_plan"]["action"] == "long"
    assert "WEEKEND OVERRIDE" in INDEPENDENT_INSTRUCTIONS


@pytest.mark.asyncio
async def test_scheduled_daily_analysis_executes_on_weekend(monkeypatch):
    from app.services import daily_outlook
    from app.domain import trading_calendar
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    monkeypatch.setattr(trading_calendar, "new_entries_allowed", lambda _: False)
    reserve = AsyncMock(return_value=(42, "owner", "started"))
    execute = AsyncMock()
    monkeypatch.setattr(daily_outlook, "reserve_run", reserve)
    monkeypatch.setattr(daily_outlook, "execute_reserved", execute)
    await daily_outlook.daily_outlook_tick()
    reserve.assert_awaited_once_with("scheduled", "daily")
    execute.assert_awaited_once_with(42, "owner")
