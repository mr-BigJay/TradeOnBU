from __future__ import annotations

from app.domain.candles import Candle
from app.services.ict_confirmation_engine import all_required_met, process_bar_sequential


def _bar(ms: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=ms, open=o, high=h, low=l, close=c, volume=1.0)


def test_wrong_order_does_not_complete():
    sequence = ["liquidity_sweep", "mss", "entry_zone_touch"]
    boundary = 1_000_000
    # POI touch before sweep — should not mark sweep or complete sequence
    bars = [
        _bar(boundary, 100, 100.5, 99.5, 100.2),
        _bar(boundary + 300_000, 100, 100.5, 99.5, 100.2),
    ]
    state: dict = {"confirmation_timestamps": {}, "completed": [], "missing": sequence}
    for bar in bars:
        window = [b for b in bars if b.time_ms <= bar.time_ms]
        progress = process_bar_sequential(
            direction="LONG",
            sequence=sequence,
            confirmation_state=state,
            bar=bar,
            window=window,
            zone_lo=99.0,
            zone_hi=101.0,
            invalidation_level=95.0,
            structured={},
            monitor_from_ms=boundary,
        )
        state = {
            "confirmation_timestamps": progress.confirmation_timestamps,
            "completed": progress.completed,
            "missing": progress.missing,
        }
    assert not all_required_met(progress)
    assert "liquidity_sweep" in progress.missing


def test_pre_creation_bar_ignored():
    sequence = ["entry_zone_touch"]
    boundary = 1_000_000
    pre = _bar(boundary - 300_000, 100, 101, 99, 100.5)
    post = _bar(boundary, 100, 100.5, 99.5, 100.2)
    progress = process_bar_sequential(
        direction="LONG",
        sequence=sequence,
        confirmation_state={},
        bar=pre,
        window=[pre],
        zone_lo=99.0,
        zone_hi=101.0,
        invalidation_level=95.0,
        structured={},
        monitor_from_ms=boundary,
    )
    assert not progress.completed
    progress2 = process_bar_sequential(
        direction="LONG",
        sequence=sequence,
        confirmation_state={"confirmation_timestamps": progress.confirmation_timestamps},
        bar=post,
        window=[post],
        zone_lo=99.0,
        zone_hi=101.0,
        invalidation_level=95.0,
        structured={},
        monitor_from_ms=boundary,
    )
    assert "entry_zone_touch" in progress2.completed
