"""Non-configurable safety floors for automatic paper trading."""

from __future__ import annotations

# Automatic paper orders must meet this net R:R after spread/fees/slippage (ExecutionModel).
HARD_MIN_AUTO_PAPER_NET_RR: float = 2.0

# A *gross price-distance* R:R ceiling for newly created AUTO-PAPER orders.
# The AI setup itself (and its analytical TP) is never modified.
MAX_NEW_AUTO_PAPER_GROSS_RR: float = 3.0


def cap_new_auto_paper_tp(
    direction: str, entry: float, stop_loss: float, proposed_tp: float
) -> float:
    """Return the executable TP within 3R, never moving an already-closer target.

    Existing positions do not pass through this function: it is used only as a
    new auto-paper OrderRequest is constructed. LONG and SHORT use the
    *planned entry* and structural SL. Does not enforce a fixed profit target
    or alter the original AI suggestion.
    """
    import math

    if not all(math.isfinite(v) for v in (entry, stop_loss, proposed_tp)):
        raise ValueError("non_finite_trade_level")
    side = direction.upper()
    if side == "LONG":
        if not stop_loss < entry < proposed_tp:
            raise ValueError("invalid_long_levels")
        ceiling = entry + MAX_NEW_AUTO_PAPER_GROSS_RR * (entry - stop_loss)
        return min(proposed_tp, ceiling)
    if side == "SHORT":
        if not proposed_tp < entry < stop_loss:
            raise ValueError("invalid_short_levels")
        floor = entry - MAX_NEW_AUTO_PAPER_GROSS_RR * (stop_loss - entry)
        return max(proposed_tp, floor)
    raise ValueError("invalid_direction")
