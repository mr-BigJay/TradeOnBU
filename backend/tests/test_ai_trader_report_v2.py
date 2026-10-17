"""AI Trader report v2: prompt + server-side report assembly."""

import json

from app.services.ai_ict_analysis import INSTRUCTIONS
from app.services.ai_trader_instructions import AI_TRADER_MODE, ICT_TRADER_INSTRUCTIONS
from app.services.ai_trader_report import build_trader_report_fa, sanitize_trader_text


def test_instructions_discretionary_trader_not_validator():
    assert AI_TRADER_MODE is True
    assert INSTRUCTIONS is ICT_TRADER_INSTRUCTIONS
    assert "NOT a scalper" in INSTRUCTIONS or "NOT a data auditor" in INSTRUCTIONS
    assert "INTRADAY" in INSTRUCTIONS
    assert "0.30%" in INSTRUCTIONS
    assert "1:2" in INSTRUCTIONS
    assert "ict_liquidity" in INSTRUCTIONS
    assert "TradeOnBU builds report" in INSTRUCTIONS


def test_build_report_decision_and_setup_first():
    data = {
        "decision": "LONG",
        "final_ai_decision": "LONG",
        "current_price": 83505,
        "market_bias": "Bullish HTF pullback",
        "entry_zone_low": 83200,
        "entry_zone_high": 83300,
        "stop_loss": 82900,
        "take_profit": 84200,
        "take_profit_2": 84500,
        "risk_reward": "1:2.7",
        "entry_confirmation": "5m MSS bullish + FVG retest",
        "invalidation_condition": "Below 82900",
        "ict_liquidity": "SSL swept at 82400",
        "ict_structure": "MSS bullish on 5m",
        "ict_displacement": "Strong up candle after sweep",
        "ict_fvg": "FVG 83100-83200",
        "ict_order_block": "OB at 83050",
        "previous_setups_review_fa": "Previous setup:\nLONG 83250\nResult:\nNot triggered",
    }
    report = build_trader_report_fa(data)
    assert report.startswith("تحلیل ICT برای BTCUSDT")
    decision_idx = report.index("تصمیم:")
    setup_idx = report.index("ستاپ:")
    entry_idx = report.index("ورود:")
    assert decision_idx < setup_idx < entry_idx
    assert "83200 - 83300" in report
    assert "82900" in report
    assert "84200" in report
    assert "5m MSS bullish" in report
    assert "lifecycle_phase" not in report.lower()


def test_build_report_no_trade_concise():
    data = {
        "decision": "NO_TRADE",
        "final_ai_decision": "NO_TRADE",
        "current_price": 100,
        "market_bias": "Range",
        "no_trade_reasons": ["No liquidity sweep", "RR below 1:2"],
        "waiting_for": ["5m BOS + FVG"],
    }
    report = build_trader_report_fa(data)
    assert "عدم ورود (NO_TRADE)" in report
    assert "دلایل عدم ورود:" in report
    assert "در انتظار:" in report
    assert "ستاپ:" not in report or report.index("تصمیم:") < report.find("Setup:") if "ستاپ:" in report else True


def test_sanitize_strips_internal_tokens():
    dirty = "Price ok but lifecycle_phase was entry_touched and first_touch_ms=123"
    clean = sanitize_trader_text(dirty)
    assert "lifecycle_phase" not in clean
    assert "first_touch_ms" not in clean


def test_long_structured_json_fields_supported():
    payload = json.loads(
        """
        {
          "decision": "SHORT",
          "final_ai_decision": "SHORT",
          "current_price": 90000,
          "market_bias": "Bearish",
          "entry_price": 90100,
          "entry_zone_low": 90050,
          "entry_zone_high": 90150,
          "stop_loss": 90500,
          "take_profit": 89000,
          "risk_reward": "1:2.2",
          "proposed_setup": {"new_setup": true}
        }
        """
    )
    report = build_trader_report_fa(payload)
    assert "SHORT" in report
    assert "90050 - 90150" in report
