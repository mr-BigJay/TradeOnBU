"""Authoritative executable swing regime shared by the analyst and paper planner.

The labels are computed from confirmed swings, never from EMA commentary.
"""
from app.services.verified_trade_plan import trend
from app.services.ict_structure import find_swings

METHOD = "verified_trade_plan.trend/swing-high-low/v1"


def executable_regime(series):
    """Expose exactly the swing observations used by compile_plan to the AI."""
    frames = {}
    for tf in ("1h", "4h"):
        bars = series[tf]
        pivots = find_swings(bars)
        frames[tf] = {
            "trend": trend(bars),
            "last_two_highs": [{"time_ms":p.time_ms, "price":p.price}
                               for p in pivots if p.kind == "HIGH"][-2:],
            "last_two_lows": [{"time_ms":p.time_ms, "price":p.price}
                              for p in pivots if p.kind == "LOW"][-2:],
        }
    return {
        "method": METHOD,
        "definition": ("Higher highs AND higher lows => bullish; lower highs AND lower lows "
                       "=> bearish; insufficient confirmed pivots => unclear; otherwise range. "
                       "Confirmed pivot uses two closed candles on each side."),
        "timeframes": frames,
        "continuation_rule": ("LONG requires 1h bullish and 4h not bearish; "
                              "SHORT requires 1h bearish and 4h not bullish. "
                              "EMA/RSI cannot override these execution labels."),
    }
