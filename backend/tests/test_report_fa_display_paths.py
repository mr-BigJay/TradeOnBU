"""User-visible surfaces must use finalized report_fa only (not raw model text)."""

from app.services.ai_trader_report import structured_for_user_display


def test_structured_for_user_display_strips_internal_model_report():
    structured = {
        "decision": "LONG",
        "report_fa": "BTCUSDT ICT Analysis\nDecision:\nLONG",
        "_artifacts": {
            "internal": {"model_report_fa_raw": "memory.open_setups lifecycle_phase audit"},
            "inference": {"requested_model": "gpt-6.1-sol"},
        },
    }
    public = structured_for_user_display(structured)
    assert "internal" not in (public.get("_artifacts") or {})
    assert public["report_fa"].startswith("BTCUSDT")
    assert public["_artifacts"]["inference"]["requested_model"] == "gpt-6.1-sol"
