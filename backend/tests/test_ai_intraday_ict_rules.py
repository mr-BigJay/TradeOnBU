"""Intraday ICT prompt rules and non-overriding safety warnings."""

from unittest.mock import patch

from app.services.ai_ict_analysis import INSTRUCTIONS, _paper_safety_notes
from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS
from app.services.ai_trader_report import build_trader_report_fa


def test_conditional_setup_rendered_when_immediate_entry_not_valid():
    data = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 83100,
        "conditional_entry_zone_high": 83200,
        "confirmation_required": "5m bullish MSS + FVG retest after SSL sweep",
        "conditional_invalidation": "Below 82900",
        "current_price": 83500,
        "market_bias": "Bullish pullback",
        "ict_liquidity": "SSL at 82400",
    }
    report = build_trader_report_fa(data)
    assert "Conditional Setup:" in report
    assert "Potential Direction:" in report
    assert "LONG" in report
    assert "83100 - 83200" in report
    assert "Confirmation required:" in report
    assert "MSS" in report
    assert "Invalidation:" in report


def test_instructions_require_conditional_plan_not_only_no_trade():
    assert "conditional setup" in INSTRUCTIONS.lower()
    assert "potential_direction" in INSTRUCTIONS
    assert "confirmation_required" in INSTRUCTIONS
    assert "confirmation_type" in INSTRUCTIONS
    assert "structural_invalidation_level" in INSTRUCTIONS


def test_instructions_require_intraday_not_scalp():
    t = ICT_TRADER_INSTRUCTIONS
    assert "INTRADAY" in t
    assert "HTF liquidity target" in t
    assert "ICT invalidation" in t
    assert "Do NOT reject a valid ICT setup only because a percentage" in t
    assert "sl_tight_structural" in t
    assert "1:2" in t
    assert "RR alone is NOT enough" in t
    assert "Clear liquidity event" in t
    assert "POI reaction" in t
    assert "Structural confirmation" in t
    assert INSTRUCTIONS is ICT_TRADER_INSTRUCTIONS


def test_paper_safety_warns_tight_scalp_levels_without_changing_decision():
    data = {
        "decision": "LONG",
        "entry_price": 83375.0,
        "stop_loss": 83295.0,
        "take_profit": 83565.0,
    }
    with patch("app.services.ai_ict_analysis.get_effective_settings") as gs:
        gs.return_value.min_net_rr = 1.5
        out = _paper_safety_notes(data, live=83380.0)
    assert out["ai_decision"] == "LONG"
    assert any("sl_below_guideline_review_noise" in w for w in out["warnings"])
    assert any("tp1_move" in w for w in out["warnings"])


def test_paper_safety_structural_tight_sl_different_warning():
    data = {
        "decision": "LONG",
        "entry_price": 83375.0,
        "stop_loss": 83295.0,
        "take_profit": 85000.0,
        "sl_tight_structural": True,
        "tp_liquidity_target": "HTF BSL 85000",
    }
    with patch("app.services.ai_ict_analysis.get_effective_settings") as gs:
        gs.return_value.min_net_rr = 1.5
        out = _paper_safety_notes(data, live=83380.0)
    assert out["ai_decision"] == "LONG"
    assert any("sl_tight_structural" in w for w in out["warnings"])
