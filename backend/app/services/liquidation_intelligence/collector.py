from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import get_settings
from app.db import SessionLocal
from app.services.liquidation_intelligence.parsers import (
    LiquidationParseError,
    parse_binance_force_order_message,
    parse_bybit_all_liquidation_message,
)
from app.services.liquidation_intelligence.repository import insert_liquidation_event, record_source_message
from app.services.liquidation_intelligence.ws_common import run_ws_message_loop

logger = logging.getLogger(__name__)

BYBIT_WS_DEFAULT = "wss://stream.bybit.com/v5/public/linear"
BINANCE_WS_DEFAULT = "wss://fstream.binance.com/market/ws/btcusdt@forceOrder"


class LiquidationIntelligenceCollector:
    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._bybit_disconnects = 0
        self._binance_disconnects = 0
        self._bybit_connected = False
        self._binance_connected = False
        self._bybit_subscribed = False
        self._binance_subscribed = False
        self._last_bybit_event_ms: int | None = None
        self._last_binance_event_ms: int | None = None
        self._last_bybit_heartbeat_ms: int | None = None
        self._last_binance_heartbeat_ms: int | None = None
        self._last_bybit_activity_ms: int | None = None
        self._last_binance_activity_ms: int | None = None

    def _settings(self):
        return get_settings()

    def _binance_ws_url(self) -> str:
        return (self._settings().liquidation_binance_ws_url or BINANCE_WS_DEFAULT).strip()

    def _bybit_ws_url(self) -> str:
        return (self._settings().liquidation_bybit_ws_url or BYBIT_WS_DEFAULT).strip()

    def status_payload(self) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        stale_after = int(self._settings().liquidation_stale_after_seconds) * 1000

        def block(
            exchange: str,
            *,
            connected: bool,
            subscribed: bool,
            last_ev: int | None,
            last_hb: int | None,
            last_act: int | None,
        ) -> dict[str, Any]:
            from app.services.liquidation_intelligence.health import connection_activity_state, effective_connection_activity_ms

            eff = effective_connection_activity_ms(
                last_connection_activity_at_ms=last_act,
                last_heartbeat_at_ms=last_hb,
                last_event_at_ms=last_ev,
            )
            activity = connection_activity_state(
                connected=connected,
                last_connection_activity_at_ms=eff,
                last_heartbeat_at_ms=last_hb,
                last_event_at_ms=last_ev,
                now_ms=now_ms,
                stale_after_ms=stale_after,
                subscription_confirmed=subscribed,
            )
            return {
                "connection_state": activity,
                "connected": connected,
                "subscription_confirmed": subscribed,
                "last_event_at_ms": last_ev,
                "last_heartbeat_at_ms": last_hb,
                "last_connection_activity_at_ms": eff,
            }

        return {
            "enabled": self._settings().liquidation_intelligence_enabled,
            "symbol": self.symbol,
            "endpoints": {"bybit": self._bybit_ws_url(), "binance": self._binance_ws_url()},
            "bybit": block(
                "bybit",
                connected=self._bybit_connected,
                subscribed=self._bybit_subscribed,
                last_ev=self._last_bybit_event_ms,
                last_hb=self._last_bybit_heartbeat_ms,
                last_act=self._last_bybit_activity_ms,
            ),
            "binance": block(
                "binance",
                connected=self._binance_connected,
                subscribed=self._binance_subscribed,
                last_ev=self._last_binance_event_ms,
                last_hb=self._last_binance_heartbeat_ms,
                last_act=self._last_binance_activity_ms,
            ),
            "reconnect_count": {"bybit": self._bybit_disconnects, "binance": self._binance_disconnects},
        }

    def start(self) -> None:
        if self._running or not self._settings().liquidation_intelligence_enabled:
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
        self._bybit_connected = False
        self._binance_connected = False
        async with SessionLocal() as session:
            await record_source_message(session, "bybit", connected=False)
            await record_source_message(session, "binance", connected=False)
            await session.commit()

    async def _touch_activity(self, exchange: str, *, at_ms: int | None = None) -> None:
        now_ms = int(at_ms or time.time() * 1000)
        if exchange == "bybit":
            self._last_bybit_activity_ms = now_ms
            self._last_bybit_heartbeat_ms = now_ms
        else:
            self._last_binance_activity_ms = now_ms
            self._last_binance_heartbeat_ms = now_ms

    async def _persist_events(self, exchange: str, events: list) -> None:
        if not events:
            return
        await self._touch_activity(exchange)
        async with SessionLocal() as session:
            for ev in events:
                try:
                    inserted, updated = await insert_liquidation_event(session, ev)
                    activity_ms = int(time.time() * 1000)
                    await self._touch_activity(exchange, at_ms=activity_ms)
                    if inserted or updated:
                        await record_source_message(
                            session,
                            exchange,
                            connected=True,
                            received_delta=1 if inserted else 0,
                            duplicate_delta=0 if inserted or updated else 1,
                            message_at_ms=ev.event_timestamp_ms,
                            connection_activity_at_ms=activity_ms,
                        )
                        if exchange == "bybit":
                            self._last_bybit_event_ms = ev.event_timestamp_ms
                        else:
                            self._last_binance_event_ms = ev.event_timestamp_ms
                    else:
                        await record_source_message(
                            session,
                            exchange,
                            connected=True,
                            duplicate_delta=1,
                            connection_activity_at_ms=activity_ms,
                        )
                except Exception:
                    logger.exception("Liquidation persist failed")
                    await record_source_message(session, exchange, connected=True, rejected_delta=1, error="persist_failed")
            await session.commit()

    async def _heartbeat(self, exchange: str) -> None:
        now_ms = int(time.time() * 1000)
        await self._touch_activity(exchange, at_ms=now_ms)
        async with SessionLocal() as session:
            await record_source_message(
                session,
                exchange,
                connected=True,
                heartbeat_at_ms=now_ms,
                connection_activity_at_ms=now_ms,
            )
            await session.commit()

    async def _on_connect(self, exchange: str, *, subscription_confirmed: bool = False) -> None:
        now_ms = int(time.time() * 1000)
        if exchange == "bybit":
            self._bybit_connected = True
            self._last_bybit_heartbeat_ms = now_ms
            if subscription_confirmed:
                self._bybit_subscribed = True
        else:
            self._binance_connected = True
            self._last_binance_heartbeat_ms = now_ms
            if subscription_confirmed:
                self._binance_subscribed = True
        await self._touch_activity(exchange, at_ms=now_ms)
        async with SessionLocal() as session:
            await record_source_message(
                session,
                exchange,
                connected=True,
                heartbeat_at_ms=now_ms,
                connection_activity_at_ms=now_ms,
                subscription_confirmed=subscription_confirmed if subscription_confirmed else None,
            )
            await session.commit()

    async def _backoff_sleep(self, attempt: int) -> None:
        base = min(120, 2 ** min(attempt, 6))
        await asyncio.sleep(base + random.uniform(0, 1.5))

    def _ws_connect_kwargs(self) -> dict[str, Any]:
        max_q = max(16, int(self._settings().liquidation_ws_max_queue))
        return {
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_queue": max_q,
        }

    async def _run_bybit_loop(self) -> None:
        attempt = 0
        topic = f"allLiquidation.{self.symbol}"
        recv_idle = float(self._settings().liquidation_ws_recv_idle_seconds)
        while self._running:
            try:
                async with websockets.connect(self._bybit_ws_url(), **self._ws_connect_kwargs()) as ws:
                    attempt = 0
                    self._bybit_subscribed = False
                    await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                    await self._on_connect("bybit")

                    async def on_msg(msg: dict[str, Any]) -> None:
                        if msg.get("op") == "pong":
                            await self._heartbeat("bybit")
                            return
                        await self._heartbeat("bybit")
                        if msg.get("success") is True:
                            self._bybit_subscribed = True
                            await self._on_connect("bybit", subscription_confirmed=True)
                            return
                        try:
                            events = parse_bybit_all_liquidation_message(msg, symbol=self.symbol)
                            if events:
                                await self._persist_events("bybit", events)
                        except LiquidationParseError:
                            async with SessionLocal() as session:
                                await record_source_message(session, "bybit", connected=True, rejected_delta=1, error="parse_error")
                                await session.commit()

                    await run_ws_message_loop(
                        ws,
                        on_json_message=on_msg,
                        on_idle=lambda: self._heartbeat("bybit"),
                        recv_idle_seconds=recv_idle,
                        running=lambda: self._running,
                    )
            except asyncio.CancelledError:
                break
            except ConnectionClosed as exc:
                self._bybit_connected = False
                self._bybit_disconnects += 1
                attempt += 1
                logger.warning("Bybit liquidation WS closed: %s", exc)
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
            except Exception as exc:
                self._bybit_connected = False
                self._bybit_disconnects += 1
                attempt += 1
                logger.exception("Bybit liquidation WS error")
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
        recv_idle = float(self._settings().liquidation_ws_recv_idle_seconds)
        while self._running:
            try:
                async with websockets.connect(self._binance_ws_url(), **self._ws_connect_kwargs()) as ws:
                    attempt = 0
                    self._binance_subscribed = True
                    await self._on_connect("binance", subscription_confirmed=True)

                    async def on_msg(msg: dict[str, Any]) -> None:
                        await self._heartbeat("binance")
                        try:
                            ev = parse_binance_force_order_message(msg, symbol=self.symbol)
                            if ev:
                                await self._persist_events("binance", [ev])
                        except LiquidationParseError:
                            async with SessionLocal() as session:
                                await record_source_message(session, "binance", connected=True, rejected_delta=1, error="parse_error")
                                await session.commit()

                    await run_ws_message_loop(
                        ws,
                        on_json_message=on_msg,
                        on_idle=lambda: self._heartbeat("binance"),
                        recv_idle_seconds=recv_idle,
                        running=lambda: self._running,
                    )
            except asyncio.CancelledError:
                break
            except ConnectionClosed as exc:
                self._binance_connected = False
                self._binance_disconnects += 1
                attempt += 1
                logger.warning("Binance liquidation WS closed: %s", exc)
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
            except Exception as exc:
                self._binance_connected = False
                self._binance_disconnects += 1
                attempt += 1
                logger.exception("Binance liquidation WS error")
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
