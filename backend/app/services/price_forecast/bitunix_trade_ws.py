from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

import websockets
from app.config import get_settings
from app.db import SessionLocal
from app.models import MarketMicroRollup

logger = logging.getLogger(__name__)

BUCKET_5M_MS = 5 * 60 * 1000
BUCKET_1M_MS = 60 * 1000


class BitunixMicroDataCollector:
    """Persist trade (CVD) and periodic depth summaries across restarts."""

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._trade_buckets: dict[int, dict[str, float]] = defaultdict(lambda: {"buy_volume": 0.0, "sell_volume": 0.0, "trade_count": 0})
        self._running = False
        self._task: asyncio.Task | None = None

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

    async def _run_loop(self) -> None:
        settings = get_settings()
        url = "wss://fapi.bitunix.com/public/"
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    sub = {"op": "subscribe", "args": [{"symbol": self.symbol, "ch": "trade"}]}
                    await ws.send(json.dumps(sub))
                    flush_at = time.monotonic() + 60
                    while self._running:
                        if time.monotonic() >= flush_at:
                            await self._flush_trades()
                            await self._snapshot_depth()
                            flush_at = time.monotonic() + 60
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if msg.get("ch") != "trade":
                            continue
                        for tr in msg.get("data") or []:
                            self._ingest_trade(tr)
            except Exception:
                logger.exception("Bitunix trade WS reconnecting")
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
        ts_raw = tr.get("t") or tr.get("ts")
        ts_ms = int(ts_raw) if isinstance(ts_raw, (int, float)) else int(time.time() * 1000)
        bucket = (ts_ms // BUCKET_5M_MS) * BUCKET_5M_MS
        b = self._trade_buckets[bucket]
        b["trade_count"] += 1
        if side == "buy":
            b["buy_volume"] += vol
        else:
            b["sell_volume"] += vol

    async def _flush_trades(self) -> None:
        if not self._trade_buckets:
            return
        buckets = dict(self._trade_buckets)
        self._trade_buckets.clear()
        async with SessionLocal() as session:
            for bucket_ms, stats in buckets.items():
                session.add(
                    MarketMicroRollup(
                        symbol=self.symbol,
                        bucket_start_ms=bucket_ms,
                        kind="trade_5m",
                        source="bitunix_ws_trades",
                        payload={
                            "buy_volume": stats["buy_volume"],
                            "sell_volume": stats["sell_volume"],
                            "trade_count": stats["trade_count"],
                        },
                    )
                )
            try:
                await session.commit()
            except Exception:
                await session.rollback()

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
                session.add(
                    MarketMicroRollup(
                        symbol=self.symbol,
                        bucket_start_ms=bucket_ms,
                        kind="depth_1m",
                        source="bitunix",
                        payload=summary,
                    )
                )
                await session.commit()
        except Exception:
            logger.debug("Depth snapshot skipped", exc_info=True)


_collector: BitunixMicroDataCollector | None = None


def get_micro_collector() -> BitunixMicroDataCollector:
    global _collector
    if _collector is None:
        _collector = BitunixMicroDataCollector()
    return _collector
