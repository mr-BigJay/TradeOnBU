from __future__ import annotations

from unittest.mock import patch

from app.domain.candles import Candle
from app.domain.opportunity_miss_reasons import REPLAY_STATUS_NOT_RUN, REPLAY_STATUS_OK
from app.services.confirmation_replay import replay_confirmations_on_5m_bars
from app.services.ict_confirmation_engine import ConfirmationProgress
from app.services.opportunity_miss_report import _replay_trusted, derive_replay_status
from app.services.revision_replay import (
    assess_revision_replay_trust,
    compute_snapshot_replay_from_ms,
)


def _candle(open_ms: int) -> Candle:
    return Candle(time_ms=open_ms, open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0)


def test_derive_replay_status_not_run_when_diff_missing():
    assert derive_replay_status(None) == REPLAY_STATUS_NOT_RUN
    assert derive_replay_status({}) == REPLAY_STATUS_NOT_RUN
    assert not _replay_trusted(None)
    assert not _replay_trusted({"replay_status": REPLAY_STATUS_NOT_RUN})


def test_compute_snapshot_replay_from_ms_after_last_merged_confirmation():
    seed = {
        "confirmation_timestamps": {"mss": 1_000_000},
        "confirmation_evidence": {},
    }
    assert (
        compute_snapshot_replay_from_ms(
            monitor_from_ms=1_000_000,
            seed=seed,
            revision_at_ms=900_000,
        )
        == 1_300_000
    )


def test_seeded_replay_does_not_process_bars_before_snapshot_boundary():
    boundary = 1_000_000
    bars = [_candle(boundary), _candle(boundary + 300_000), _candle(boundary + 600_000)]
    seed = {
        "confirmation_timestamps": {"mss": boundary},
        "confirmation_evidence": {},
    }
    replay_from = boundary + 300_000
    processed: list[int] = []

    def _track(**kwargs: object) -> ConfirmationProgress:
        bar = kwargs["bar"]
        assert isinstance(bar, Candle)
        processed.append(bar.time_ms)
        state = kwargs["confirmation_state"]
        assert isinstance(state, dict)
        ts = dict(state.get("confirmation_timestamps") or {})
        ts["liquidity_sweep"] = bar.time_ms
        return ConfirmationProgress(
            sequence=["liquidity_sweep", "mss"],
            confirmation_timestamps=ts,
            completed=["liquidity_sweep", "mss"],
            missing=[],
        )

    with patch("app.services.confirmation_replay.process_bar_sequential", side_effect=_track):
        replay_confirmations_on_5m_bars(
            direction="LONG",
            sequence=["liquidity_sweep", "mss"],
            closed_bars=bars,
            zone_lo=99.0,
            zone_hi=101.0,
            invalidation_level=90.0,
            structured={},
            monitor_from_ms=boundary,
            initial_confirmation_state=seed,
            replay_from_ms=replay_from,
        )

    assert processed == [boundary + 300_000, boundary + 600_000]
    assert boundary not in processed


def test_seeded_replay_without_revision_timestamp_is_fail_closed():
    conf = {
        "sequence": ["liquidity_sweep", "mss"],
        "confirmation_timestamps": {"mss": 1_000_000},
    }
    notes = {
        "revision_history": [
            {
                "impact": "sequence",
                "sequence_before": ["mss"],
                "sequence_after": ["liquidity_sweep", "mss"],
                "confirmation_snapshot_after_merge": conf,
            }
        ]
    }
    status, notes_out, seed, replay_from = assess_revision_replay_trust(
        confirmation_state=conf,
        objective_notes=notes,
        monitor_from_ms=1_000_000,
    )
    assert status != REPLAY_STATUS_OK
    assert seed is None
    assert replay_from is None
    assert any("timestamp" in n for n in notes_out)
