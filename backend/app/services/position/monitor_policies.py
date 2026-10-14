from __future__ import annotations

from app.domain.candles import Candle

EXECUTION_NORMAL = "normal"
EXECUTION_SUSPENDED = "suspended"
EXECUTION_UNRESOLVED = "unresolved"

POLICY_SAME_BAR_ENTRY = "same_bar_entry_no_intrabar_sequence"
POLICY_AMBIGUOUS_INTRABAR = "bar_close"
POLICY_GAP_UNRESOLVED = "missing_history_hold"


def levels_touched_on_bar(direction: str, bar: Candle, sl: float, tp: float) -> tuple[bool, bool]:
    if direction == "LONG":
        return bar.low <= sl, bar.high >= tp
    return bar.high >= sl, bar.low <= tp


def same_bar_exit_ambiguous_after_entry(
    direction: str,
    bar: Candle,
    entry_price: float,
    sl: float,
    tp: float,
) -> bool:
    """
    After a limit fill on this bar, OHLC cannot prove TP/SL happened after entry.
    Any SL/TP touch on the entry bar is treated as ambiguous.
    """
    sl_t, tp_t = levels_touched_on_bar(direction, bar, sl, tp)
    return sl_t or tp_t
