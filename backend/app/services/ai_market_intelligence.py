"""Compact, read-only public exchange context for ICT AI analysis.

This module never calls exchange APIs or any LLM. It uses the 15-minute
public snapshots persisted independently of the retired price-forecast feature.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketDataSnapshot

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_AGE_SECONDS = 25 * 60
MAX_OI_AGE_SECONDS = 150 * 60  # Binance public 1h history can trail the live hour.
MAX_TRADE_BUCKET_AGE_SECONDS = 15 * 60
MAX_ORDER_BOOK_AGE_SECONDS = 180
MAX_CONTEXT_CHARS = 2600


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _age_ms(now_ms: int, event_ms: Any) -> int | None:
    timestamp = _number(event_ms)
    if timestamp is None:
        return None
    delta = now_ms - int(timestamp)
    return delta if delta >= 0 else None


def _funding(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("status") != "ok" or raw.get("source") != "bitunix":
        return None
    rate = _number(raw.get("funding_rate"))
    if rate is None:
        return None
    return {"source": "bitunix", "funding_rate": rate}


def _open_interest(raw: Any, *, now_ms: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("status") != "ok" or raw.get("source") != "binance_futures":
        return None
    if raw.get("period") != "1h":
        return None
    points = raw.get("points")
    if not isinstance(points, list):
        return None
    valid = []
    for p in points[-12:]:
        if not isinstance(p, dict):
            continue
        age = _age_ms(now_ms, p.get("time_ms"))
        oi = _number(p.get("open_interest"))
        if age is None or age > MAX_OI_AGE_SECONDS * 1000 or oi is None or oi <= 0:
            continue
        valid.append((int(p["time_ms"]), oi))
    valid = sorted(set(valid))
    if not valid:
        return None
    t, last = valid[-1]
    out: dict[str, Any] = {
        "source": "binance_futures",
        "market": "USD-M",
        "period": "1h",
        "latest_time_ms": t,
        "open_interest": last,
    }
    if len(valid) >= 2 and valid[-2][1] > 0:
        out["change_from_previous_pct"] = round((last / valid[-2][1] - 1) * 100, 3)
    return out


def _trade_flow(raw: Any, *, now_ms: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("status") != "ok" or raw.get("source") != "bitunix_ws_trades":
        return None
    if raw.get("interpretation") != "provisional_volume_delta_not_verified_cvd":
        return None
    buckets = raw.get("buckets_5m")
    if not isinstance(buckets, list) or not buckets:
        return None
    last = buckets[-1]
    if not isinstance(last, dict):
        return None
    age = _age_ms(now_ms, last.get("bucket_start_ms"))
    if age is None or age > MAX_TRADE_BUCKET_AGE_SECONDS * 1000:
        return None
    buy, sell = _number(last.get("buy_volume")), _number(last.get("sell_volume"))
    if buy is None or sell is None or buy < 0 or sell < 0:
        return None
    collector = raw.get("collector") or {}
    if not isinstance(collector, dict) or collector.get("freshness") != "ok":
        return None
    return {
        "source": "bitunix_ws_trades",
        "bucket_start_ms": int(last["bucket_start_ms"]),
        "buy_volume": buy,
        "sell_volume": sell,
        "volume_delta": round(buy - sell, 4),
        "interpretation": "provisional_side_delta_not_verified_cvd",
        "gap_detected": bool(collector.get("gap_detected")),
    }


def _liquidations(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("status") not in ("ok", "partial"):
        return None
    data: dict[str, Any] = {}
    for source in ("bybit", "binance"):
        block = raw.get(source) or {}
        if not isinstance(block, dict) or block.get("status") not in ("ok", "partial"):
            continue
        quality = block.get("event_query_quality") or {}
        if quality.get("truncated"):
            continue
        one_hour = (block.get("windows") or {}).get("1h") or {}
        coverage = one_hour.get("coverage")
        if coverage in (None, "unknown", "empty", "observed_none", "truncated") or one_hour.get("query_truncated"):
            continue
        metrics = one_hour.get("metrics") or {}
        if not isinstance(metrics, dict):
            continue
        long_usd = _number(metrics.get("long_liquidation_notional_usd"))
        short_usd = _number(metrics.get("short_liquidation_notional_usd"))
        if long_usd is None or short_usd is None or long_usd < 0 or short_usd < 0:
            continue
        data[source] = {
            "window": "1h",
            "coverage": coverage,
            "partial": bool(one_hour.get("partial")),
            "source_quality": block.get("source_quality"),
            "long_liquidated_usd_observed": round(long_usd, 2),
            "short_liquidated_usd_observed": round(short_usd, 2),
            "price_semantics": "bankruptcy_price" if source == "bybit" else "average_fill_price_sampled",
        }
    if not data:
        return None
    return {
        "interpretation": "historical_observed_events_only_not_future_liquidation_levels",
        "not_market_wide_total": True,
        "sources": data,
    }


def _order_book(raw: Any, *, snapshot_age_seconds: int) -> dict[str, Any] | None:
    if snapshot_age_seconds > MAX_ORDER_BOOK_AGE_SECONDS:
        return None
    if not isinstance(raw, dict) or raw.get("status") != "ok" or raw.get("source") != "bitunix":
        return None
    imbalance = _number(raw.get("imbalance_ratio"))
    if imbalance is None or abs(imbalance) > 1:
        return None
    return {
        "source": "bitunix",
        "top15_imbalance_ratio": imbalance,
        "age_seconds": snapshot_age_seconds,
        "interpretation": "mutable_visible_orders_not_guaranteed_executable_liquidity",
    }


def compact_snapshot(snapshot: MarketDataSnapshot, *, symbol: str, now: datetime | None = None) -> dict[str, Any]:
    """Drop old/mismatched/invalid sources; bound input size; never invent zero metrics."""
    now = _utc(now or datetime.now(timezone.utc))
    if not snapshot.valid or snapshot.symbol != symbol or not snapshot.collected_at:
        return {"status": "unavailable"}
    age = (now - _utc(snapshot.collected_at)).total_seconds()
    if not (0 <= age <= MAX_SNAPSHOT_AGE_SECONDS):
        return {"status": "unavailable", "reason": "snapshot_stale_or_future"}

    payload = snapshot.payload or {}
    if not isinstance(payload, dict) or payload.get("symbol") != symbol:
        return {"status": "unavailable", "reason": "snapshot_symbol_mismatch"}

    now_ms = int(now.timestamp() * 1000)
    candidates = {
        "funding": _funding(payload.get("funding")),
        "open_interest": _open_interest(payload.get("open_interest"), now_ms=now_ms),
        "trade_flow": _trade_flow(payload.get("trade_flow"), now_ms=now_ms),
        "liquidations": _liquidations(payload.get("liquidations")),
        "order_book": _order_book(payload.get("order_book"), snapshot_age_seconds=int(age)),
    }
    sources = {key: value for key, value in candidates.items() if value is not None}
    if not sources:
        return {"status": "unavailable", "reason": "no_fresh_usable_sources"}

    result = {
        "status": "available",
        "symbol": symbol,
        "snapshot_at": _utc(snapshot.collected_at).isoformat(),
        "snapshot_age_seconds": int(age),
        "usage": "secondary_context_only_not_trade_confirmation",
        "sources": sources,
    }
    import json

    # Fail closed rather than silently trim arbitrary fields and their quality labels.
    if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        return {"status": "unavailable", "reason": "context_size_limit"}
    return result


async def load_ai_market_intelligence(session: AsyncSession, *, symbol: str) -> dict[str, Any]:
    """Read only the most recent persisted public snapshot; zero external requests."""
    try:
        row = (
            await session.execute(
                select(MarketDataSnapshot)
                .where(MarketDataSnapshot.symbol == symbol, MarketDataSnapshot.valid.is_(True))
                .order_by(desc(MarketDataSnapshot.collected_at), desc(MarketDataSnapshot.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return {"status": "unavailable", "reason": "no_public_snapshot"}
        return compact_snapshot(row, symbol=symbol)
    except Exception:
        logger.exception("Optional public market intelligence skipped for %s", symbol)
        return {"status": "unavailable", "reason": "snapshot_read_failed"}
