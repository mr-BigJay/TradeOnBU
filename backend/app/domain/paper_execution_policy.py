"""Non-configurable safety floors for automatic paper trading."""

from __future__ import annotations

# Automatic paper orders must meet this net R:R after spread/fees/slippage (ExecutionModel).
HARD_MIN_AUTO_PAPER_NET_RR: float = 2.0
