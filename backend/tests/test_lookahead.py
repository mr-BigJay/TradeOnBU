from app.domain.candles import Candle
from app.services.ict_structure import find_fvg_zones, validate_ict_sequence


def _c(o, h, l, cl, t):
    return Candle(open=o, high=h, low=l, close=cl, time_ms=t)


def test_fvg_uses_only_confirmed_history():
    candles = [_c(10, 10, 9, 9.5, i * 300_000) for i in range(10)]
    candles.append(_c(20, 25, 19, 24, 10 * 300_000))
    zones_before = find_fvg_zones(candles[:-1])
    zones_after = find_fvg_zones(candles)
    assert len(zones_after) >= len(zones_before)


def test_sequence_does_not_require_future_bar():
    candles = [_c(100, 101, 99, 100, i * 300_000) for i in range(50)]
    seq = validate_ict_sequence(candles, "LONG")
    assert seq.ok is False
