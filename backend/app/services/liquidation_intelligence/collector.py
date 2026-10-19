from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import websockets

from app.config import get_settings
from app.db import SessionLocal
from app.services.liquidation_intelligence.parsers import (
    LiquidationParseError,
    parse_binance_force_order_message,
    parse_bybit_all_liquidation_message,
)
from app.services.liquidation_intelligence.repository import insert_liquidation_event, record_source_message

logger = logging.getLogger(__name__)

BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS = "wss://fstream.binance.com/ws/btcusdt@forceOrder"


class LiquidationIntelligenceCollector:
    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._bybit_disconnects = 0
        self._binance_disconnects = 0
        self._last_bybit_msg_ms: int | None = None
        self._last_binance_msg_ms: int | None = None

    def status_payload(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        stale_after = int(get_settings().liquidation_stale_after_seconds) * 1000

        def fresh(last: int | None) -> str:
            if last is None:
                return "unavailable"
            return "ok" if (now_ms - last) <= stale_after else "stale"

        return {
            "enabled": get_settings().liquidation_intelligence_enabled,
            "symbol": self.symbol,
            "bybit": {"freshness": fresh(self._last_bybit_msg_ms), "last_message_at_ms": self._last_bybit_msg_ms},
            "binance": {"freshness": fresh(self._last_binance_msg_ms), "last_message_at_ms": self._last_binance_msg_ms},
            "reconnect_count": {"bybit": self._bybit_disconnects, "binance": self._binance_disconnects},
        }

    def start(self) -> None:
        if self._running or not get_settings().liquidation_intelligence_enabled:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._run_bybit_loop(), name="liq-bybit"),
            asyncio.create_task(self._run_binance_loop(), name="liq-binance"),
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        async with SessionLocal() as session:
            await record_source_message(session, "bybit", connected=False)
            await record_source_message(session, "binance", connected=False)
            await session.commit()

    async def _persist_events(self, exchange: str, events: list) -> None:
        if not events:
            return
        async with SessionLocal() as session:
            for ev in events:
                try:
                    inserted = await insert_liquidation_event(session, ev)
                    if inserted:
                        await record_source_message(
                            session,
                            exchange,
                            connected=True,
                            received_delta=1,
                            message_at_ms=ev.event_timestamp_ms,
                        )
                    else:
                        await record_source_message(session, exchange, connected=True, duplicate_delta=1)
                except Exception:
                    logger.exception("Liquidation persist failed")
                    await record_source_message(session, exchange, connected=True, rejected_delta=1, error="persist_failed")
            await session.commit()

    async def _backoff_sleep(self, attempt: int) -> None:
        base = min(120, 2 ** min(attempt, 6))
        await asyncio.sleep(base + random.uniform(0, 1.5))

    async def _run_bybit_loop(self) -> None:
        attempt = 0
        topic = f"allLiquidation.{self.symbol}"
        while self._running:
            try:
                async with websockets.connect(BYBIT_WS, ping_interval=20, ping_timeout=20) as ws:
                    attempt = 0
                    await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    async with SessionLocal() as session:
                        await record_source_message(session, "bybit", connected=True)
                        await session.commit()
                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        msg = json.loads(raw)
                        if msg.get("op") == "pong" or msg.get("success") is True:
                            continue
                        try:
                            events = parse_bybit_all_liquidation_message(msg, symbol=self.symbol)
                            if events:
                                self._last_bybit_msg_ms = int(time.time() * 1000)
                                await self._persist_events("bybit", events)
                        except LiquidationParseError:
                            async with SessionLocal() as session:
                                await record_source_message(session, "bybit", connected=True, rejected_delta=1, error="parse_error")
                                await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._bybit_disconnects += 1
                attempt += 1
                logger.exception("Bybit liquidation WS disconnected")
                async with SessionLocal() as session:
                    await record_source_message(
                        session,
                        "bybit",
                        connected=False,
                        reconnect_delta=1,
                        error=str(exc)[:200],
                    )
                    await session.commit()
                await self._backoff_sleep(attempt)

    async def _run_binance_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20, ping_timeout=20) as ws:
                    attempt = 0
                    async with SessionLocal() as session:
                        await record_source_message(session, "binance", connected=True)
                        await session.commit()
                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        msg = json.loads(raw)
                        try:
                            ev = parse_binance_force_order_message(msg, symbol=self.symbol)
                            if ev:
                                self._last_binance_msg_ms = int(time.time() * 1000)
                                await self._persist_events("binance", [ev])
                        except LiquidationParseError:
                            async with SessionLocal() as session:
                                await record_source_message(session, "binance", connected=True, rejected_delta=1, error="parse_error")
                                await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._binance_disconnects += 1
                attempt += 1
                logger.exception("Binance liquidation WS disconnected")
                async with SessionLocal() as session:
                    await record_source_message(
                        session,
                        "binance",
                        connected=False,
                        reconnect_delta=1,
                        error=str(exc)[:200],
                    )
                    await session.commit()
                await self._backoff_sleep(attempt)


_collector: LiquidationIntelligenceCollector | None = None


def get_liquidation_collector() -> LiquidationIntelligenceCollector:
    global _collector
    if _collector is None:
        _collector = LiquidationIntelligenceCollector()
    return _collector
