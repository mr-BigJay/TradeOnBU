"""Fail-closed live price gate for auto paper entry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from app.domain.paper_execution_policy import MAX_LIVE_TICKER_AGE_SECONDS


@dataclass(frozen=True)
class LivePriceAssessment:
    ok: bool
    price: float | None
    reason: str | None
    ticker_age_seconds: float | None = None


def validate_live_price_for_auto_paper(
    live_price: float | None,
    *,
    ticker_time_ms: int | None = None,
    now: datetime | None = None,
    max_age_seconds: float = MAX_LIVE_TICKER_AGE_SECONDS,
) -> LivePriceAssessment:
    if live_price is None or not math.isfinite(live_price) or live_price <= 0:
        return LivePriceAssessment(ok=False, price=live_price, reason="live_price_unavailable")
    now = now or datetime.now(timezone.utc)
    if ticker_time_ms is not None and ticker_time_ms > 0:
        age = max(0.0, (now.timestamp() * 1000 - ticker_time_ms) / 1000.0)
        if age > max_age_seconds:
            return LivePriceAssessment(
                ok=False,
                price=live_price,
                reason="live_price_stale",
                ticker_age_seconds=age,
            )
        return LivePriceAssessment(ok=True, price=live_price, reason=None, ticker_age_seconds=age)
    return LivePriceAssessment(ok=False, price=live_price, reason="live_price_unreliable_no_timestamp")
