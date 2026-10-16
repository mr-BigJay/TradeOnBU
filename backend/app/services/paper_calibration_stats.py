"""Wilson intervals and walk-forward holdout helpers (no fabricated rates)."""

from __future__ import annotations

import math
from dataclasses import dataclass


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) / n) + z2 / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


@dataclass
class WalkForwardSplit:
    train_n: int
    train_rate: float | None
    oos_n: int
    oos_rate: float | None


def walk_forward_binary_rate(
    outcomes: list[bool],
    *,
    train_fraction: float = 0.7,
    min_oos: int = 5,
) -> WalkForwardSplit:
    """Time-ordered outcomes: train on prefix, report OOS on suffix."""
    if not outcomes:
        return WalkForwardSplit(0, None, 0, None)
    split = max(1, int(len(outcomes) * train_fraction))
    if len(outcomes) - split < min_oos:
        split = max(1, len(outcomes) - min_oos) if len(outcomes) > min_oos else len(outcomes)
    train = outcomes[:split]
    oos = outcomes[split:]
    train_rate = sum(train) / len(train) if train else None
    oos_rate = sum(oos) / len(oos) if oos else None
    return WalkForwardSplit(len(train), train_rate, len(oos), oos_rate)
