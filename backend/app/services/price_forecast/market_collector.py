from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketDataSnapshot, MarketMicroRollup
from app.services.market_data import MarketDataService
from app.services.price_forecast.binance_supplement import (
    fetch_binance_liquidations_snapshot,
    fetch_binance_open_interest_hist,
)


def _candle_row(c) -> list[float | int]:
    return [int(c.time_ms), float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume or 0)]


def _summarize_depth(raw: dict[str, Any]) -> dict[str, Any]:
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    bid_notional = 0.0
    ask_notional = 0.0
    for px, qty in bids[:15]:
        bid_notional += float(px) * float(qty)
    for px, qty in asks[:15]:
        ask_notional += float(px) * float(qty)
    total = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / total if total > 0 else 0.0
    return {
        "bid_notional_top15": round(bid_notional, 2),
        "ask_notional_top15": round(ask_notional, 2),
        "imbalance_ratio": round(imbalance, 4),
        "best_bid": float(bids[0][0]) if bids else None,
        "best_ask": float(asks[0][0]) if asks else None,
        "levels_used": min(15, len(bids), len(asks)),
    }


async def _fetch_bitunix_funding(base: str, symbol: str) -> dict[str, Any]:
    from app.services.price_forecast.bitunix_funding import parse_bitunix_funding_body

    url = f"{base}/api/v1/futures/market/funding_rate"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params={"symbol": symbol})
        resp.raise_for_status()
        body = resp.json()
    return parse_bitunix_funding_body(body, symbol)


async def _fetch_bitunix_depth(base: str, symbol: str) -> dict[str, Any]:
    url = f"{base}/api/v1/futures/market/depth"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params={"symbol": symbol, "limit": "15"})
        resp.raise_for_status()
        body = resp.json()
    if body.get("code") != 0:
        return {"status": "unavailable", "source": "bitunix", "reason": body.get("msg")}
    summary = _summarize_depth(body.get("data") or {})
    summary["status"] = "ok"
    summary["source"] = "bitunix"
    return summary


async def _load_trade_rollups(session: AsyncSession, symbol: str, *, limit: int = 24) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(MarketMicroRollup)
            .where(MarketMicroRollup.symbol == symbol, MarketMicroRollup.kind == "trade_5m")
            .order_by(desc(MarketMicroRollup.bucket_start_ms))
            .limit(limit)
        )
    ).scalars().all()
    from app.services.price_forecast.bitunix_trade_ws import get_micro_collector

    collector_meta = get_micro_collector().status_payload()
    if not rows:
        return {
            "status": "unavailable",
            "source": "bitunix_ws_trades",
            "reason": "no_persisted_trade_rollups",
            "interpretation": "provisional_volume_delta_not_verified_cvd",
            "note": "Bitunix trade channel side (buy/sell) treated as provisional taker-side until verified",
            "collector": collector_meta,
        }
    points = []
    cvd = 0.0
    for row in reversed(rows):
        p = row.payload or {}
        buy_v = float(p.get("buy_volume") or 0)
        sell_v = float(p.get("sell_volume") or 0)
        delta = buy_v - sell_v
        cvd += delta
        points.append(
            {
                "bucket_start_ms": row.bucket_start_ms,
                "buy_volume": buy_v,
                "sell_volume": sell_v,
                "volume_delta": delta,
                "cvd_cumulative": round(cvd, 4),
                "trade_count": int(p.get("trade_count") or 0),
            }
        )
    return {
        "status": "ok" if collector_meta.get("freshness") == "ok" else "stale",
        "source": "bitunix_ws_trades",
        "interpretation": "provisional_volume_delta_not_verified_cvd",
        "aggressor_field": "side_buy_sell_per_bitunix_trade_channel",
        "buckets_5m": points[-12:],
        "collector": collector_meta,
    }


async def _load_depth_rollups(session: AsyncSession, symbol: str, *, limit: int = 12) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(MarketMicroRollup)
            .where(MarketMicroRollup.symbol == symbol, MarketMicroRollup.kind == "depth_1m")
            .order_by(desc(MarketMicroRollup.bucket_start_ms))
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return {"status": "unavailable", "source": "bitunix", "reason": "no_depth_rollups"}
    return {
        "status": "ok",
        "source": "bitunix",
        "snapshots_1m": [{"bucket_start_ms": r.bucket_start_ms, **(r.payload or {})} for r in reversed(rows)],
    }


async def collect_market_snapshot(session: AsyncSession, *, symbol: str = "BTCUSDT") -> MarketDataSnapshot:
    market = MarketDataService()
    now = datetime.now(timezone.utc)
    freshness: dict[str, Any] = {}
    payload: dict[str, Any] = {"symbol": symbol, "collected_at": now.isoformat()}

    try:
        quote = await market.fetch_live_price(symbol)
        quote_stale = market.is_quote_stale(quote)
        payload["live_price"] = {
            "price": quote.price,
            "source": quote.source,
            "stale": bool(quote.stale or quote_stale),
            "receipt_time_ms": quote.receipt_time_ms,
            "exchange_time_ms": quote.exchange_time_ms,
        }
        freshness["live_price"] = "stale" if (quote.stale or quote_stale) else "ok"
    except Exception as exc:
        payload["live_price"] = {"status": "unavailable", "error": str(exc)[:200]}
        freshness["live_price"] = "unavailable"

    kline_limits = {"1h": 48, "15m": 32, "5m": 36}
    payload["klines"] = {}
    for interval, lim in kline_limits.items():
        try:
            bundle = await market.fetch_kline_bundle(symbol, interval, lim)
            payload["klines"][interval] = {
                "status": "ok" if bundle.data_complete else "incomplete",
                "closed": [_candle_row(c) for c in bundle.closed[-lim:]],
                "forming": _candle_row(bundle.forming) if bundle.forming else None,
                "gaps_remaining": len(bundle.gaps_remaining),
            }
            freshness[f"kline_{interval}"] = "ok" if bundle.data_complete else "stale"
        except Exception as exc:
            payload["klines"][interval] = {"status": "unavailable", "error": str(exc)[:200]}
            freshness[f"kline_{interval}"] = "unavailable"

    base = market.settings.bitunix_futures_base
    try:
        payload["funding"] = await _fetch_bitunix_funding(base, symbol)
        freshness["funding"] = payload["funding"].get("status", "unavailable")
    except Exception as exc:
        payload["funding"] = {"status": "unavailable", "source": "bitunix", "error": str(exc)[:200]}
        freshness["funding"] = "unavailable"

    try:
        payload["order_book"] = await _fetch_bitunix_depth(base, symbol)
        freshness["order_book"] = payload["order_book"].get("status", "unavailable")
    except Exception as exc:
        payload["order_book"] = {"status": "unavailable", "source": "bitunix", "error": str(exc)[:200]}
        freshness["order_book"] = "unavailable"

    payload["trade_flow"] = await _load_trade_rollups(session, symbol)
    freshness["cvd"] = payload["trade_flow"].get("status", "unavailable")
    payload["depth_history"] = await _load_depth_rollups(session, symbol)

    try:
        payload["open_interest"] = await fetch_binance_open_interest_hist(symbol)
        freshness["open_interest"] = payload["open_interest"].get("status", "unavailable")
    except Exception as exc:
        payload["open_interest"] = {
            "source": "binance_futures",
            "status": "unavailable",
            "error": str(exc)[:200],
        }
        freshness["open_interest"] = "unavailable"

    payload["liquidations"] = await fetch_binance_liquidations_snapshot(symbol)
    freshness["liquidations"] = payload["liquidations"].get("status", "unavailable")

    snap = MarketDataSnapshot(symbol=symbol, collected_at=now, payload=payload, freshness=freshness, valid=True)
    session.add(snap)
    await session.flush()
    return snap


def build_llm_market_summary(snapshot: MarketDataSnapshot, *, anchor_price: float, expected_times_iso: list[str]) -> str:
    """Compact JSON string for GPT — no raw order book ladders."""
    import json

    p = snapshot.payload or {}
    slim = {
        "symbol": snapshot.symbol,
        "collected_at": p.get("collected_at"),
        "freshness": snapshot.freshness,
        "anchor_price": anchor_price,
        "required_forecast_times_utc": expected_times_iso,
        "live_price": p.get("live_price"),
        "klines": p.get("klines"),
        "funding": p.get("funding"),
        "order_book_summary": p.get("order_book"),
        "trade_flow_summary": p.get("trade_flow"),
        "depth_history_summary": p.get("depth_history"),
        "open_interest_binance": p.get("open_interest"),
        "liquidations": p.get("liquidations"),
    }
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
