from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import websockets

from app.config import get_settings
from app.db import SessionLocal
from app.models import MarketMicroRollup
from app.services.price_forecast.trade_rollup import parse_trade_event_ms, upsert_trade_bucket

logger = logging.getLogger(__name__)

BUCKET_5M_MS = 5 * 60 * 1000
BUCKET_1M_MS = 60 * 1000
STALE_AFTER_MS = 5 * 60 * 1000
# Bitunix documents JSON application ping/pong in addition to WS control frames.
# Only one subscription and one JSON ping per 20 seconds (<< 5 msg/s limit).
APP_PING_INTERVAL_SECONDS = 20.0
APP_PONG_TIMEOUT_SECONDS = 20.0


def heartbeat_ping_payload(timestamp_seconds: int) -> str:
    return json.dumps({"op": "ping", "ping": timestamp_seconds}, separators=(",", ":"))


def matching_heartbeat_pong(message: dict[str, Any], expected_ping: int | None) -> bool:
    if expected_ping is None or message.get("op") != "ping":
        return False
    pong = message.get("pong")
    return type(pong) is int and pong == expected_ping



class BitunixMicroDataCollector:
    """Persist trade rollups and periodic depth summaries across restarts."""

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._trade_buckets: dict[int, dict[str, float]] = defaultdict(
            lambda: {"buy_volume": 0.0, "sell_volume": 0.0, "trade_count": 0}
        )
        self._running = False
        self._task: asyncio.Task | None = None
        self.ws_connected = False
        self.last_trade_at_ms: int | None = None
        self.last_flush_at_ms: int | None = None
        self.last_disconnect_at_ms: int | None = None
        self.disconnect_count = 0
        self.gap_detected = False
        self.last_app_ping_at_ms: int | None = None
        self.last_app_pong_at_ms: int | None = None
        self.heartbeat_timeout_count = 0
        self.last_disconnect_reason: str | None = None

    def status_payload(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        fresh = (
            self.ws_connected
            and self.last_trade_at_ms is not None
            and (now_ms - self.last_trade_at_ms) <= STALE_AFTER_MS
        )
        return {
            "ws_connected": self.ws_connected,
            "freshness": "ok" if fresh else ("stale" if self.last_trade_at_ms else "unavailable"),
            "last_trade_at_ms": self.last_trade_at_ms,
            "last_flush_at_ms": self.last_flush_at_ms,
            "last_disconnect_at_ms": self.last_disconnect_at_ms,
            "disconnect_count": self.disconnect_count,
            "gap_detected": self.gap_detected,
            "pending_buckets": len(self._trade_buckets),
            "last_app_ping_at_ms": self.last_app_ping_at_ms,
            "last_app_pong_at_ms": self.last_app_pong_at_ms,
            "heartbeat_timeout_count": self.heartbeat_timeout_count,
            "last_disconnect_reason": self.last_disconnect_reason,
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _stream_connected(self, ws) -> None:
        """Collect trades with a documented Bitunix application-level heartbeat.

        WS control-frame pings from the library are not a substitute for the
        exchange's JSON ping. Never interpret a ping/pong response as a trade.
        """
        sub = {"op": "subscribe", "args": [{"symbol": self.symbol, "ch": "trade"}]}
        await ws.send(json.dumps(sub))
        flush_at = time.monotonic() + 60
        next_ping_at = time.monotonic() + APP_PING_INTERVAL_SECONDS
        pending_ping: int | None = None
        pending_since: float | None = None

        while self._running:
            now = time.monotonic()
            if pending_ping is not None and pending_since is not None:
                if now - pending_since >= APP_PONG_TIMEOUT_SECONDS:
                    self.heartbeat_timeout_count += 1
                    raise TimeoutError("bitunix_app_pong_timeout")
            if now >= next_ping_at and pending_ping is None:
                stamp = int(time.time())
                await ws.send(heartbeat_ping_payload(stamp))
                self.last_app_ping_at_ms = int(time.time() * 1000)
                pending_ping = stamp
                pending_since = time.monotonic()
                next_ping_at = pending_since + APP_PING_INTERVAL_SECONDS

            if time.monotonic() >= flush_at:
                await self._flush_trades()
                await self._snapshot_depth()
                flush_at = time.monotonic() + 60
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                continue
            if msg.get("op") == "ping":
                if matching_heartbeat_pong(msg, pending_ping):
                    self.last_app_pong_at_ms = int(time.time() * 1000)
                    pending_ping = None
                    pending_since = None
                continue
            if msg.get("ch") != "trade" or msg.get("symbol") not in (None, self.symbol):
                continue
            for tr in msg.get("data") or []:
                if isinstance(tr, dict):
                    self._ingest_trade(tr)

    async def _run_loop(self) -> None:
        url = "wss://fapi.bitunix.com/public/"
        while self._running:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20
                ) as ws:
                    self.ws_connected = True
                    self.gap_detected = self.disconnect_count > 0
                    self.last_disconnect_reason = None
                    await self._stream_connected(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_disconnect_at_ms = int(time.time() * 1000)
                self.disconnect_count += 1
                self.gap_detected = True
                self.last_disconnect_reason = str(exc)[:180] or type(exc).__name__
                if isinstance(exc, (TimeoutError, websockets.exceptions.ConnectionClosed)):
                    logger.warning(
                        "Bitunix trade WS reconnecting: %s", self.last_disconnect_reason
                    )
                else:
                    logger.exception("Bitunix trade WS reconnecting")
            finally:
                self.ws_connected = False
                # A reconnect must not drop a partial 5m trade bucket. This
                # upsert adds only currently unflushed events; failed DB writes
                # remain in memory and are retried on the next flush.
                await self._flush_trades()
            if self._running:
                await asyncio.sleep(5)

    def _ingest_trade(self, tr: dict[str, Any]) -> None:
        side = str(tr.get("s") or "").lower()
        if side not in ("buy", "sell"):
            return
        try:
            vol = float(tr.get("v") or 0)
        except (TypeError, ValueError):
            return
        if vol <= 0:
            return
        ts_ms = parse_trade_event_ms(tr)
        if ts_ms is None:
            return
        self.last_trade_at_ms = ts_ms
        bucket = (ts_ms // BUCKET_5M_MS) * BUCKET_5M_MS
        b = self._trade_buckets[bucket]
        b["trade_count"] += 1
        if side == "buy":
            b["buy_volume"] += vol
        else:
            b["sell_volume"] += vol

    def _snapshot_pending_buckets(self) -> dict[int, dict[str, float]]:
        return {
            bucket_ms: {
                "buy_volume": float(stats["buy_volume"]),
                "sell_volume": float(stats["sell_volume"]),
                "trade_count": float(stats["trade_count"]),
            }
            for bucket_ms, stats in self._trade_buckets.items()
        }

    def _subtract_flushed(self, pending: dict[int, dict[str, float]]) -> None:
        for bucket_ms, stats in pending.items():
            live = self._trade_buckets.get(bucket_ms)
            if live is None:
                continue
            live["buy_volume"] -= stats["buy_volume"]
            live["sell_volume"] -= stats["sell_volume"]
            live["trade_count"] -= stats["trade_count"]
            if live["trade_count"] <= 0 and live["buy_volume"] <= 1e-12 and live["sell_volume"] <= 1e-12:
                del self._trade_buckets[bucket_ms]

    async def _flush_trades(self) -> None:
        if not self._trade_buckets:
            return
        pending = self._snapshot_pending_buckets()
        if not pending:
            return
        async with SessionLocal() as session:
            try:
                for bucket_ms, stats in pending.items():
                    await upsert_trade_bucket(
                        session,
                        symbol=self.symbol,
                        bucket_start_ms=bucket_ms,
                        buy_volume=float(stats["buy_volume"]),
                        sell_volume=float(stats["sell_volume"]),
                        trade_count=int(stats["trade_count"]),
                    )
                await session.commit()
            except Exception:
                await session.rollback()
                return
        self._subtract_flushed(pending)
        self.last_flush_at_ms = int(time.time() * 1000)

    async def _snapshot_depth(self) -> None:
        import httpx

        settings = get_settings()
        url = f"{settings.bitunix_futures_base}/api/v1/futures/market/depth"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"symbol": self.symbol, "limit": "15"})
                resp.raise_for_status()
                body = resp.json()
            if body.get("code") != 0:
                return
            from app.services.price_forecast.market_collector import _summarize_depth

            summary = _summarize_depth(body.get("data") or {})
            bucket_ms = (int(time.time() * 1000) // BUCKET_1M_MS) * BUCKET_1M_MS
            async with SessionLocal() as session:
                from sqlalchemy import select

                existing = (
                    await session.execute(
                        select(MarketMicroRollup).where(
                            MarketMicroRollup.symbol == self.symbol,
                            MarketMicroRollup.bucket_start_ms == bucket_ms,
                            MarketMicroRollup.kind == "depth_1m",
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.payload = summary
                else:
                    session.add(
                        MarketMicroRollup(
                            symbol=self.symbol,
                            bucket_start_ms=bucket_ms,
                            kind="depth_1m",
                            source="bitunix",
                            payload=summary,
                        )
                    )
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
        except Exception:
            logger.debug("Depth snapshot skipped", exc_info=True)


_collector: BitunixMicroDataCollector | None = None


def get_micro_collector() -> BitunixMicroDataCollector:
    global _collector
    if _collector is None:
        _collector = BitunixMicroDataCollector()
    return _collector
