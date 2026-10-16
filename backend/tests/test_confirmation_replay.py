from __future__ import annotations

from app.domain.candles import Candle
from app.services.confirmation_replay import diff_live_vs_replay
from app.services.ict_confirmation_engine import ConfirmationProgress


def test_diff_live_vs_replay_detects_missing_confirmations():
    replay = ConfirmationProgress(
        sequence=["a", "b"],
        completed=["a", "b"],
        missing=[],
        confirmation_timestamps={"a": 1000, "b": 2000},
    )
    diff = diff_live_vs_replay(
        setup_id="s1",
        live_state={"confirmation_timestamps": {"a": 1000}},
        replay=replay,
    )
    assert diff.missing_in_live == ["b"]
    assert diff.order_mismatch is True


def test_diff_notes_multi_token_without_1m_proof():
    from app.domain.opportunity_miss_reasons import REPLAY_STATUS_OK

    replay = ConfirmationProgress(sequence=["a"], confirmation_timestamps={})
    multi = [
        {
            "bar_open_ms": 5000,
            "tokens_on_5m_close_window": ["sweep", "mss"],
            "chronology_provable_on_1m": False,
        }
    ]
    diff = diff_live_vs_replay(
        setup_id="s1",
        live_state={},
        replay=replay,
        multi_candidates=multi,
        replay_status=REPLAY_STATUS_OK,
    )
    assert any("multi-token" in n for n in diff.notes)
