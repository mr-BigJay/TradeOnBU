"""Bitunix observed carried-open compatibility; never modify traded extremes.

Not a claim about all exchanges: an out-of-range open requires exact adjacent
close evidence. The close itself must always be within the reported range.
"""
import math


def price_shape(bar):
    return all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
               for x in (bar.open, bar.high, bar.low, bar.close)) and bar.low <= bar.close <= bar.high


def carried_open(bar, previous, step):
    return (price_shape(bar) and not bar.low <= bar.open <= bar.high
            and previous is not None and price_shape(previous)
            and bar.time_ms - previous.time_ms == step
            and bar.open == previous.close)


def acceptable_prices(bar, previous=None, step=None):
    return price_shape(bar) and (bar.low <= bar.open <= bar.high
        or bar.carried_open_verified
        or (step is not None and carried_open(bar, previous, step)))


def displacement_body(bar, sign):
    # Clip only the analytical body origin, NOT the raw candle or execution range.
    # A carried close outside this interval is not evidence of displacement here.
    origin = min(bar.high, max(bar.low, bar.open))
    return sign * (bar.close - origin)
