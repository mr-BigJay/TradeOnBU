"""Descriptive closed-bar metrics, computed locally with no execution thresholds."""
import math


def _ema(values, period):
    value = sum(values[:period]) / period
    for x in values[period:]:
        value += 2 / (period + 1) * (x - value)
    return value


def _wilder(values, period=14):
    if len(values) < period:
        return []
    result = [sum(values[:period]) / period]
    for x in values[period:]:
        result.append((result[-1] * (period - 1) + x) / period)
    return result


def _rsi_series(values, period=14):
    """Wilder smoothing seeded by the first period of close-to-close changes.

    An entirely flat series has undefined relative strength: keep it missing,
    rather than sending a fabricated momentum reading to the model.
    """
    changes = [b-a for a,b in zip(values, values[1:])]
    gains = _wilder([max(x, 0) for x in changes], period)
    losses = _wilder([max(-x, 0) for x in changes], period)
    return [None if gain == loss == 0 else
            100.0 if loss == 0 else 100 - 100 / (1 + gain/loss)
            for gain,loss in zip(gains,losses)]


def closed_bar_features(bars):
    if len(bars) < 30:
        return {"available": False, "reason": "insufficient_closed_history"}
    if any(not all(math.isfinite(v) for v in (b.open, b.high, b.low, b.close)) for b in bars):
        return {"available": False, "reason": "invalid_ohlc"}
    closes = [b.close for b in bars]
    tr, plus, minus = [], [], []
    for prev, bar in zip(bars, bars[1:]):
        tr.append(max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close)))
        up, down = bar.high - prev.high, prev.low - bar.low
        plus.append(max(up, 0) if up > down else 0)
        minus.append(max(down, 0) if down > up else 0)
    atrs = _wilder(tr)
    dx = [100 * abs(p - m) / (p + m) if p + m else 0
          for p, m in zip(_wilder(plus), _wilder(minus))]
    adx = _wilder(dx)
    atr = atrs[-1]
    last = bars[-1]
    volumes = [b.volume for b in bars[-21:-1]]
    volume_ok = all(v is not None and math.isfinite(v) and v >= 0 for v in volumes)
    avg_volume = sum(volumes) / len(volumes) if volume_ok else 0
    rvol = last.volume / avg_volume if avg_volume > 0 and last.volume is not None and math.isfinite(last.volume) and last.volume >= 0 else None
    ema = _ema(closes, 20)
    ema9 = _ema(closes, 9)
    previous20, previous9 = _ema(closes[:-1], 20), _ema(closes[:-1], 9)
    rsi = _rsi_series(closes)
    rsi_change = rsi[-1]-rsi[-2] if rsi[-1] is not None and rsi[-2] is not None else None
    relation = "above" if ema9 > ema else "below" if ema9 < ema else "equal"
    cross = "up" if previous9 <= previous20 and ema9 > ema else "down" if previous9 >= previous20 and ema9 < ema else "none"
    return {
        "available": True, "last_closed_open_ms": last.time_ms,
        "history_count": len(bars), "close": last.close,
        "ema9": ema9, "ema9_change": ema9-previous9,
        "ema9_ema20_relation": relation, "ema9_ema20_cross": cross,
        "close_ema9_atr": (last.close-ema9)/atr if atr > 0 else None,
        "rsi14": rsi[-1], "rsi14_change": rsi_change,
        "rsi14_recent": [{"open_ms": b.time_ms, "value": value}
                         for b,value in zip(bars[-5:],rsi[-5:])],
        "atr14": atr, "adx14": adx[-1] if adx else None,
        "ema20": ema, "ema20_change": ema - _ema(closes[:-1], 20),
        "body_atr": abs(last.close - last.open) / atr if atr > 0 else None,
        "close_ema_atr": (last.close - ema) / atr if atr > 0 else None,
        "relative_volume20": rvol,
        "prior20_high": max(b.high for b in bars[-21:-1]),
        "prior20_low": min(b.low for b in bars[-21:-1]),
    }


def strategy_features(candles_by_interval, *, timeframes=("4h", "1h", "15m", "5m")):
    from app.domain.candle_utils import interval_ms

    def for_interval(tf):
        bars = candles_by_interval.get(tf, [])
        if any(b.time_ms - a.time_ms != interval_ms(tf) for a, b in zip(bars, bars[1:])):
            return {"available": False, "reason": "non_contiguous_closed_history"}
        return closed_bar_features(bars)

    return {"mode": "descriptive_only_no_entry_gate", "version": 2,
        "calculation": "Closed Bitunix candles; EMA SMA seed, alpha=2/(n+1); RSI Wilder RMA, SMA seed; flat RSI=null. Changes are one candle; cross is descriptive, not an entry condition.",
        "timeframes": {
        tf: for_interval(tf)
        for tf in timeframes
    }}
