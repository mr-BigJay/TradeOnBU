from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

WINDOW_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}


@dataclass
class _Acc:
    long_notional: float = 0.0
    short_notional: float = 0.0
    event_count: int = 0
    largest: float = 0.0

    def add(self, side: str, notional: float) -> None:
        self.event_count += 1
        self.largest = max(self.largest, notional)
        if side == "long":
            self.long_notional += notional
        else:
            self.short_notional += notional

    def to_dict(self) -> dict[str, Any]:
        total = self.long_notional + self.short_notional
        ratio = (self.long_notional / self.short_notional) if self.short_notional > 0 else None
        return {
            "long_liquidation_notional_usd": round(self.long_notional, 2),
            "short_liquidation_notional_usd": round(self.short_notional, 2),
            "total_observed_notional_usd": round(total, 2),
            "long_short_ratio": round(ratio, 4) if ratio is not None else None,
            "event_count": self.event_count,
            "largest_observed_usd": round(self.largest, 2),
        }


def _events_in_window(events: list[Any], now_ms: int, window_ms: int) -> list[Any]:
    start = now_ms - window_ms
    return [e for e in events if int(e.event_timestamp_ms) >= start]


def compute_window_metrics(events: list[Any], now_ms: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, ms in WINDOW_MS.items():
        acc = _Acc()
        for ev in _events_in_window(events, now_ms, ms):
            acc.add(str(ev.liquidated_position_side), float(ev.estimated_notional_usd))
        out[name] = acc.to_dict()
    return out


def detect_burst(current_total: float, baseline_total: float) -> dict[str, Any]:
    if baseline_total <= 0:
        return {"burst": False, "intensity_vs_baseline": None}
    intensity = current_total / baseline_total
    return {"burst": intensity >= 2.5, "intensity_vs_baseline": round(intensity, 3)}


def compute_price_clusters(events: list[Any], *, bucket_usd: float = 250.0, limit: int = 8) -> list[dict[str, Any]]:
    clusters: dict[int, _Acc] = defaultdict(_Acc)
    times: dict[int, tuple[int, int]] = {}
    for ev in events:
        price = float(ev.price)
        if not math.isfinite(price) or price <= 0:
            continue
        bucket = int(price // bucket_usd) * int(bucket_usd)
        side = str(ev.liquidated_position_side)
        notional = float(ev.estimated_notional_usd)
        clusters[bucket].add(side, notional)
        t = int(ev.event_timestamp_ms)
        if bucket not in times:
            times[bucket] = (t, t)
        else:
            times[bucket] = (min(times[bucket][0], t), max(times[bucket][1], t))
    ranked = sorted(clusters.items(), key=lambda kv: kv[1].long_notional + kv[1].short_notional, reverse=True)[:limit]
    result = []
    for bucket, acc in ranked:
        lo, hi = bucket, bucket + bucket_usd
        first_ms, last_ms = times.get(bucket, (0, 0))
        result.append(
            {
                "price_range_usd": [lo, hi],
                "observed_notional_usd": round(acc.long_notional + acc.short_notional, 2),
                "long_notional_usd": round(acc.long_notional, 2),
                "short_notional_usd": round(acc.short_notional, 2),
                "event_count": acc.event_count,
                "first_event_ms": first_ms,
                "last_event_ms": last_ms,
            }
        )
    return result


def build_exchange_analytics(events: list[Any], now_ms: int) -> dict[str, Any]:
    windows = compute_window_metrics(events, now_ms)
    w5 = windows.get("5m", {})
    w15 = windows.get("15m", {})
    prev5_total = max(0.0, float(w15.get("total_observed_notional_usd") or 0) - float(w5.get("total_observed_notional_usd") or 0))
    burst = detect_burst(float(w5.get("total_observed_notional_usd") or 0), prev5_total / 2 if prev5_total else 0)
    return {
        "windows": windows,
        "burst_metrics": burst,
        "price_clusters": compute_price_clusters(_events_in_window(events, now_ms, WINDOW_MS["1h"])),
    }
