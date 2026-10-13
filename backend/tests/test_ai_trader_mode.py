"""AI Trader Mode: prompt, memory context, and structured trade output."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models import AiProposedSetup
from app.services.ai_ict_analysis import INSTRUCTIONS, _paper_safety_notes
from app.services.ai_setup_memory import build_ai_memory_context, serialize_setup_row
from app.services.ai_trader_instructions import AI_TRADER_MODE, ICT_TRADER_INSTRUCTIONS


def test_trader_instructions_contain_professional_ict_trader_guidance():
    text = ICT_TRADER_INSTRUCTIONS
    assert AI_TRADER_MODE is True
    assert "ICT professional trader" in text
    assert "Make a trading decision" in text
    assert "BSL/SSL" in text
    assert "Do not force trades" in text
    assert "Previous Setup Review" in text or "Previous setup" in text.lower()
    assert INSTRUCTIONS is ICT_TRADER_INSTRUCTIONS


def test_prompt_json_schema_supports_entry_sl_tp_and_optional_tp2():
    assert "entry_zone_low" in INSTRUCTIONS
    assert "stop_loss" in INSTRUCTIONS
    assert "take_profit" in INSTRUCTIONS
    assert "take_profit_2" in INSTRUCTIONS
    assert "decision (LONG|SHORT|NO_TRADE)" in INSTRUCTIONS
    assert "final_ai_decision" in INSTRUCTIONS


def test_paper_safety_allows_long_with_levels():
    data = {
        "decision": "LONG",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 105.0,
    }
    with patch("app.services.ai_ict_analysis.get_effective_settings") as gs:
        gs.return_value.min_net_rr = 1.5
        out = _paper_safety_notes(data, live=100.5)
    assert out["ai_decision"] == "LONG"
    assert out["paper_action"] == "ai_proposal_only"
    assert "missing_levels" not in out["warnings"]


def test_paper_safety_no_trade_unchanged():
    out = _paper_safety_notes({"decision": "NO_TRADE"}, live=100.0)
    assert out["paper_action"] == "none"


def test_serialize_setup_includes_trader_memory_fields():
    row = AiProposedSetup(
        setup_id="abc",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        entry_price=100.0,
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        stop_loss=98.0,
        take_profit=105.0,
        entry_touched=True,
        sl_touched=False,
        tp_touched=False,
        objective_notes={"lifecycle_phase": "entry_touched", "first_touch_ms": {"entry": 123}},
    )
    ser = serialize_setup_row(row)
    assert ser["direction"] == "LONG"
    assert ser["entry_price"] == 100.0
    assert ser["stop_loss"] == 98.0
    assert ser["take_profit"] == 105.0
    assert ser["entry_touched"] is True
    assert ser["lifecycle_phase"] == "entry_touched"


@pytest.mark.asyncio
async def test_memory_context_includes_previous_setups_for_ai_trader(session):
    setup = AiProposedSetup(
        setup_id="mem-1",
        run_id=2,
        symbol="BTCUSDT",
        direction="SHORT",
        status="open",
        entry_price=90000.0,
        stop_loss=91000.0,
        take_profit=88000.0,
        objective_notes={"lifecycle_phase": "entry_not_reached"},
    )
    session.add(setup)
    await session.commit()

    market = AsyncMock()
    market.fetch_historical_closed = AsyncMock(
        return_value=type(
            "B",
            (),
            {
                "closed": [],
                "forming": None,
                "gaps_detected": [],
                "gaps_remaining": [],
                "data_complete": True,
                "interval": "5m",
            },
        )()
    )

    with patch("app.services.ai_setup_memory.refresh_setup_objective_state", new=AsyncMock(return_value=[setup])):
        with patch("app.services.ai_setup_memory.load_trackable_setups", new=AsyncMock(return_value=[setup])):
            with patch("app.services.ai_setup_memory.load_recent_closed_setups", new=AsyncMock(return_value=[])):
                with patch("app.services.ai_setup_memory.load_recent_analysis_summaries", new=AsyncMock(return_value=[])):
                    memory = await build_ai_memory_context(
                        session, symbol="BTCUSDT", candles_by_interval={}, market=market
                    )

    assert memory.get("ai_trader_mode") is True
    assert len(memory["open_setups"]) == 1
    open0 = memory["open_setups"][0]
    assert open0["direction"] == "SHORT"
    assert open0["stop_loss"] == 91000.0
    assert "AI Trader Mode" in " ".join(memory["memory_rules"])


def test_example_long_structured_payload_parses():
    payload = json.loads(
        """
        {
          "decision": "LONG",
          "final_ai_decision": "LONG",
          "report_fa": "# تحلیل ICT",
          "entry_price": 83250,
          "entry_zone_low": 83100,
          "entry_zone_high": 83200,
          "stop_loss": 82900,
          "take_profit": 84200,
          "take_profit_2": 84500,
          "risk_reward": "1:2.7",
          "proposed_setup": {"new_setup": true, "thesis_fa": "pullback long"}
        }
        """
    )
    assert payload["decision"] == "LONG"
    assert payload["take_profit_2"] == 84500
