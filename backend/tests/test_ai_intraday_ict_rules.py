"""Intraday ICT prompt rules and non-overriding safety warnings."""

from unittest.mock import patch

from app.services.ai_ict_analysis import INSTRUCTIONS, _paper_safety_notes
from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS


def test_instructions_require_intraday_not_scalp():
    t = ICT_TRADER_INSTRUCTIONS
    assert "INTRADAY" in t
    assert "0.30%" in t or "0.003" in t
    assert "0.80%" in t or "0.008" in t
    assert "1:2" in t
    assert "external" in t.lower() or "BSL/SSL" in t
    assert "POI" in t or "FVG" in t
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
    assert any("sl_too_tight" in w or "sl_below_preferred" in w for w in out["warnings"])
    assert any("tp1_move_below_intraday" in w for w in out["warnings"])
