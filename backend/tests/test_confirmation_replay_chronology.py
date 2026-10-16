from __future__ import annotations

from app.domain.candles import Candle
from app.services.confirmation_replay import chronology_provable_on_1m

STEP5 = 300_000
STEP1 = 60_000
ZONE_LO, ZONE_HI = 99.9, 100.1


def _5m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def _1m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def _prior_flat(base: int, n: int = 14) -> list[Candle]:
    return [
        _5m(base - (n - i) * STEP5, o=100.0, h=100.2, l=99.0, c=100.1) for i in range(n)
    ]


def _build_positive_same_5m() -> tuple[list[Candle], list[Candle], Candle, int]:
    t5 = 20_000 * STEP5
    prior = _prior_flat(t5)
    bar_5m = _5m(t5, o=100.0, h=100.8, l=97.5, c=100.4)
    closed_5m = prior + [bar_5m]
    m0, m1, m2, m3, m4 = (t5 + i * STEP1 for i in range(5))
    closed_1m = [
        _1m(m0, o=100.0, h=99.85, l=97.5, c=99.6),
        _1m(m1, o=99.6, h=99.88, l=99.5, c=99.7),
        _1m(m2, o=100.15, h=100.25, l=100.12, c=100.18),
        _1m(m3, o=100.15, h=100.5, l=100.12, c=100.45),
        _1m(m4, o=100.2, h=100.15, l=99.95, c=100.05),
    ]
    now_ms = t5 + 5 * STEP1 + 5_000
    return closed_5m, closed_1m, bar_5m, now_ms


def test_chronology_positive_liquidity_sweep_mss_touch():
    from app.services.ict_confirmation_engine import _token_on_bar

    closed_5m, closed_1m, bar_5m, now_ms = _build_positive_same_5m()
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    hits = [t for t in seq if _token_on_bar(t, direction="LONG", window=closed_5m, zone_lo=ZONE_LO, zone_hi=ZONE_HI, structured={})]
    assert hits == seq
    ok, reason = chronology_provable_on_1m(
        direction="LONG",
        sequence=seq,
        hits_on_5m=hits,
        bar_5m=bar_5m,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        structured={},
        now_ms=now_ms,
    )
    assert ok is True, reason


def test_chronology_negative_wrong_intrabar_order():
    closed_5m, closed_1m, bar_5m, now_ms = _build_positive_same_5m()
    t5 = bar_5m.time_ms
    m0, m2, m4 = t5, t5 + 2 * STEP1, t5 + 4 * STEP1
    # MSS-like close on minute 0, sweep-like on minute 2 → order violation vs sequence
    closed_1m = [
        _1m(m0, o=100.0, h=100.5, l=99.8, c=100.4),
        _1m(t5 + STEP1, o=100.4, h=100.5, l=100.0, c=100.2),
        _1m(m2, o=100.2, h=100.3, l=98.4, c=99.7),
        _1m(t5 + 3 * STEP1, o=99.7, h=100.0, l=99.5, c=99.9),
        _1m(m4, o=99.9, h=100.6, l=99.5, c=100.2),
    ]
    from app.services.ict_confirmation_engine import _token_on_bar

    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    hits = [t for t in seq if _token_on_bar(t, direction="LONG", window=closed_5m, zone_lo=ZONE_LO, zone_hi=ZONE_HI, structured={})]
    ok, reason = chronology_provable_on_1m(
        direction="LONG",
        sequence=seq,
        hits_on_5m=hits,
        bar_5m=bar_5m,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        structured={},
        now_ms=now_ms,
    )
    assert ok is False
    assert "order_violation" in reason or "token_" in reason or reason == "5m_hits_inconsistent_with_close_window"
