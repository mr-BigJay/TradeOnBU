"""Non-configurable safety floors for automatic paper trading."""

from __future__ import annotations

# Automatic paper orders must meet this net R:R after spread/fees/slippage (ExecutionModel).
HARD_MIN_AUTO_PAPER_NET_RR: float = 2.0

# Quality execution engine caps (restart-safe; enforced in DB queries).
MAX_CONCURRENT_AUTO_PAPER_ENTRIES: int = 1
MAX_AUTO_PAPER_ENTRY_ORDERS_TEHRAN_DAY: int = 2

# Entry mode thresholds (basis points of entry price).
MARKET_ENTRY_MAX_DISTANCE_BPS: float = 20.0  # ~0.20% — immediate paper market when confirmed
LIMIT_ENTRY_MAX_DISTANCE_BPS: float = 120.0  # beyond this: skip (no chase)
# Max spread+slippage budget for market entry vs planned entry (fail closed).
MARKET_ENTRY_MAX_EXECUTION_PENALTY_BPS: float = 35.0

# Performance targets (require calibrated historical samples before enforcing as hard gates).
TARGET_FILL_RATE: float = 0.70
TARGET_WIN_RATE: float = 0.60
MIN_CALIBRATION_SAMPLES: int = 20

# Uncalibrated: limit retest beyond this distance (bps) is treated as low fill probability.
LIMIT_LOW_FILL_DISTANCE_BPS: float = 65.0
