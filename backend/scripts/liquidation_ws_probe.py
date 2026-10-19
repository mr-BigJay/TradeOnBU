#!/usr/bin/env python3
"""Read-only diagnostic: connect to Bybit/Binance liquidation WS and report connection/subscription/events."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import websockets

BYBIT_DEFAULT = "wss://stream.bybit.com/v5/public/linear"
BINANCE_DEFAULT = "wss://fstream.binance.com/market/ws/btcusdt@forceOrder"


async def probe_bybit(seconds: float, url: str) -> dict:
    connected = False
    subscribed = False
    events = 0
    heartbeats = 0
    end = time.time() + seconds
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            connected = True
            await ws.send(json.dumps({"op": "subscribe", "args": ["allLiquidation.BTCUSDT"]}))
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5, max(0.5, end - time.time())))
                except asyncio.TimeoutError:
                    heartbeats += 1
                    continue
                msg = json.loads(raw)
                if msg.get("success") is True:
                    subscribed = True
                if str(msg.get("topic", "")).startswith("allLiquidation"):
                    events += len(msg.get("data") or [])
    except Exception as exc:
        return {
            "exchange": "bybit",
            "url": url,
            "connected": connected,
            "subscription_confirmed": subscribed,
            "events_seen": events,
            "idle_heartbeats": heartbeats,
            "error": str(exc)[:200],
        }
    return {
        "exchange": "bybit",
        "url": url,
        "connected": connected,
        "subscription_confirmed": subscribed,
        "events_seen": events,
        "idle_heartbeats": heartbeats,
        "note": "Zero events during probe is not necessarily a failure",
    }


async def probe_binance(seconds: float, url: str) -> dict:
    connected = False
    subscribed = True
    events = 0
    idle = 0
    end = time.time() + seconds
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            connected = True
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5, max(0.5, end - time.time())))
                except asyncio.TimeoutError:
                    idle += 1
                    continue
                msg = json.loads(raw)
                if str(msg.get("e")).lower() == "forceorder":
                    events += 1
    except Exception as exc:
        return {
            "exchange": "binance",
            "url": url,
            "connected": connected,
            "subscription_confirmed": subscribed,
            "events_seen": events,
            "idle_timeouts": idle,
            "error": str(exc)[:200],
        }
    return {
        "exchange": "binance",
        "url": url,
        "connected": connected,
        "subscription_confirmed": subscribed,
        "events_seen": events,
        "idle_timeouts": idle,
        "note": "Zero events during probe is not necessarily a failure",
    }


async def main(seconds: float) -> None:
    bybit_url = os.environ.get("LIQUIDATION_BYBIT_WS_URL", BYBIT_DEFAULT)
    binance_url = os.environ.get("LIQUIDATION_BINANCE_WS_URL", BINANCE_DEFAULT)
    bybit, binance = await asyncio.gather(probe_bybit(seconds, bybit_url), probe_binance(seconds, binance_url))
    print(json.dumps({"bybit": bybit, "binance": binance}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    asyncio.run(main(args.seconds))
