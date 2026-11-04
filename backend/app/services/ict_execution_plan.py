"""Strategy A execution prices from closed monitor candles and Paper costs."""
import math

from app.domain.candles import Candle
from app.services.execution_model_sim import auto_paper_rr_from_settings
from app.services.ict_structure import find_swings
from app.services.ict_confirmation_engine import _token_on_bar

STEP = 300_000
REASONS = {
    "ict_structure_unconfirmed": "جمع‌آوری نقدینگی، شکست ساختار و بازگشت به محدوده به ترتیب تأیید نشده‌اند.",
    "ict_structure_data_gap": "برای بررسی ساختار، دادهٔ پیوسته و تازهٔ کندل‌ها کافی نیست.",
    "ict_stop_inside_structure": "حدضرر از نقطهٔ ابطال و انتهای کندل جمع‌آوری نقدینگی عبور نمی‌کند.",
    "ict_obstacle_before_two_r": "تا اولین مانع ساختاری پس از هزینه‌ها فضای ۲R وجود ندارد.",
}


def target_at_two_r(sim, direction, entry, stop, obstacle):
    """First BTCUSDT price tick reaching net 2R before or at the obstacle."""
    sign = 1 if direction == "LONG" else -1
    first = math.floor(entry * 10) + 1 if sign == 1 else -math.ceil(entry * 10) + 1
    last = math.floor(sign * obstacle * 10)

    def acceptable(tick):
        return auto_paper_rr_from_settings(sim, direction=direction, entry=entry,
            sl=stop, tp=sign * tick / 10, min_net_rr=2).acceptable

    if first > last or not acceptable(last):
        return None
    while first < last:
        middle = (first + last) // 2
        if acceptable(middle):
            last = middle
        else:
            first = middle + 1
    return sign * first / 10


def strict_a_target(row, sim, bars, now_ms):
    """Return the executable target or a stable reason to wait."""
    conf = row.confirmation_state or {}
    times = conf.get("confirmation_timestamps") or {}
    sweep = times.get("liquidity_sweep")
    touch = times.get("entry_zone_touch")
    if not isinstance(sweep, int) or not isinstance(touch, int) or not any(
        isinstance(times.get(key), int) and sweep < times[key] < touch for key in ("bos", "mss")
    ):
        return None, "ict_structure_unconfirmed"
    closed = sorted((b for b in bars if b.time_ms + STEP <= now_ms), key=lambda b: b.time_ms)
    expected_last = now_ms // STEP * STEP - STEP
    by_time = {b.time_ms: b for b in closed}
    if (len(closed) < 18 or closed[-1].time_ms != expected_last or sweep not in by_time
            or touch not in by_time or any(b.time_ms - a.time_ms != STEP for a, b in zip(closed, closed[1:]))):
        return None, "ict_structure_data_gap"
    long = row.direction == "LONG"
    sign = 1 if long else -1
    lo, hi = row.entry_zone_low, row.entry_zone_high
    if lo is None or hi is None or lo > hi:
        return None, "ict_structure_unconfirmed"

    def proven(token, timestamp):
        window = [b for b in closed if b.time_ms <= timestamp][-48:]
        return bool(window and window[-1].time_ms == timestamp and _token_on_bar(
            token, direction=row.direction, window=window, zone_lo=lo, zone_hi=hi,
            structured=row.structured_snapshot or {},
        ))

    if not proven("liquidity_sweep", sweep) or not proven("entry_zone_touch", touch):
        return None, "ict_structure_unconfirmed"
    if not any(isinstance(times.get(k), int) and sweep < times[k] < touch and proven(k, times[k])
               for k in ("mss", "bos")):
        return None, "ict_structure_unconfirmed"
    candle = by_time[sweep]
    entry, stop, inv, target = row.entry_price, row.stop_loss, row.invalidation_level, row.take_profit
    if (not all(v and math.isfinite(v) and v > 0 for v in (entry, stop, inv, target))
            or sign * (entry - inv) <= 0 or sign * (inv - stop) < 0
            or sign * ((candle.low if long else candle.high) - stop) <= 0):
        return None, "ict_stop_inside_structure"
    return structural_target(sim, row.direction, entry, stop, target, closed)


def structural_target(sim, direction, entry, stop, target, closed):
    long = direction == "LONG"
    sign = 1 if long else -1
    by_time = {b.time_ms: b for b in closed}
    # Aggregate only complete exchange-aligned triples. Both lists contain closed bars.
    fifteen = []
    for bar in closed:
        if bar.time_ms % (3 * STEP) == 0 and bar.time_ms + 2 * STEP in by_time:
            group = [bar, by_time[bar.time_ms + STEP], by_time[bar.time_ms + 2 * STEP]]
            fifteen.append(Candle(bar.open, max(b.high for b in group), min(b.low for b in group),
                                  group[-1].close, bar.time_ms))
    levels = [target]
    for series in (closed, fifteen):
        for pivot in find_swings(series):
            if pivot.kind != ("HIGH" if long else "LOW") or sign * (pivot.price - entry) <= 0:
                continue
            # A later close through the level invalidates this obstacle.
            if not any(sign * (b.close - pivot.price) > 0 for b in series[pivot.index + 1:]):
                levels.append(pivot.price)
    obstacle = min(levels) if long else max(levels)
    tp = target_at_two_r(sim, direction, entry, stop, obstacle)
    return (tp, None) if tp is not None else (None, "ict_obstacle_before_two_r")
