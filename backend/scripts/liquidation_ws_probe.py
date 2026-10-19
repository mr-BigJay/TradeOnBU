#!/usr/bin/env python3
"""Read-only diagnostic: connect to Bybit/Binance liquidation WS briefly and print message counts."""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets

BYBIT = "wss://stream.bybit.com/v5/public/linear"
BINANCE = "wss://fstream.binance.com/ws/btcusdt@forceOrder"


async def probe_bybit(seconds: float) -> dict:
    count = 0
    end = time.time() + seconds
    async with websockets.connect(BYBIT, ping_interval=20) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": ["allLiquidation.BTCUSDT"]}))
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(5, end - time.time()))
                msg = json.loads(raw)
                if msg.get("topic", "").startswith("allLiquidation"):
                    count += len(msg.get("data") or [])
            except asyncio.TimeoutError:
                break
    return {"exchange": "bybit", "events_seen": count}


async def probe_binance(seconds: float) -> dict:
    count = 0
    end = time.time() + seconds
    async with websockets.connect(BINANCE, ping_interval=20) as ws:
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(5, end - time.time()))
                msg = json.loads(raw)
                if str(msg.get("e")).lower() == "forceorder":
                    count += 1
            except asyncio.TimeoutError:
                break
    return {"exchange": "binance", "events_seen": count}


async def main(seconds: float) -> None:
    bybit, binance = await asyncio.gather(probe_bybit(seconds), probe_binance(seconds))
    print(json.dumps({"bybit": bybit, "binance": binance}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    asyncio.run(main(args.seconds))
