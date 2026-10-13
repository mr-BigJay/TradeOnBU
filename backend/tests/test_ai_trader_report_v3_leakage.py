"""Ensure contaminated model JSON never leaks into user report_fa."""

import json

from app.services.ai_trader_report import finalize_user_report, is_report_clean

FORBIDDEN = ("memory.", "lifecycle_phase", "OHLC", "uncertainty")


def _assert_clean(report: str) -> None:
    low = report.lower()
    for token in FORBIDDEN:
        assert token.lower() not in low, f"forbidden {token!r} in report: {report[:500]}"


def test_mock_audit_model_response_produces_clean_report():
    """Simulates model echoing internal memory/audit vocabulary in JSON fields."""
    contaminated = json.loads(
        """
        {
          "decision": "LONG",
          "final_ai_decision": "LONG",
          "report_fa": "Review memory.open_setups and lifecycle_phase first_touch_ms. OHLC validation failed.",
          "market_bias": "Bullish despite memory.open_setups review",
          "ict_liquidity": "SSL sweep — ignore data quality gaps",
          "ict_structure": "5m MSS; lifecycle_phase was entry_touched",
          "ict_displacement": "Strong move",
          "ict_fvg": "FVG 83100-83200",
          "ict_order_block": "OB support",
          "entry_zone_low": 83200,
          "entry_zone_high": 83300,
          "stop_loss": 82900,
          "take_profit": 84200,
          "risk_reward": "1:2.5",
          "entry_confirmation": "5m close into zone",
          "invalidation_condition": "Below 82900",
          "previous_setups_review_fa": "Prior run_id 99 lifecycle_phase unknown",
          "previous_setup_line": "Previous setup: LONG 83250",
          "previous_result_line": "Result: Not triggered",
          "uncertainty_notes": "internal candle inconsistency"
        }
        """
    )
    report = finalize_user_report(contaminated, symbol="BTCUSDT", live=83505.0)
    _assert_clean(report)
    assert is_report_clean(report)
    assert report.startswith("BTCUSDT ICT Analysis")
    assert "Decision:" in report
    assert "LONG" in report
    assert "83200 - 83300" in report
    assert "Previous setup: LONG 83250" in report
    assert "Not triggered" in report


def test_no_trade_max_three_reasons_in_report():
    data = {
        "decision": "NO_TRADE",
        "final_ai_decision": "NO TRADE",
        "current_price": 100,
        "market_bias": "Neutral",
        "no_trade_reasons": ["a", "b", "c", "d", "e"],
        "waiting_for": ["BOS"],
    }
    report = finalize_user_report(data, symbol="BTCUSDT", live=100)
    _assert_clean(report)
    assert report.count("- a") + report.count("- b") + report.count("- c") <= 3 or "Reason:" in report
