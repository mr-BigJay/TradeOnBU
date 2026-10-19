from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
HeartbeatHandler = Callable[[], Awaitable[None]]


async def run_ws_message_loop(
    ws: websockets.WebSocketClientProtocol,
    *,
    on_json_message: MessageHandler,
    on_idle: HeartbeatHandler | None = None,
    recv_idle_seconds: float = 120.0,
    running: Callable[[], bool] | None = None,
) -> None:
    """Read until connection closes. Recv idle timeout is NOT a disconnect."""
    while running is None or running():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=recv_idle_seconds)
        except asyncio.TimeoutError:
            if on_idle is not None:
                await on_idle()
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Liquidation WS non-JSON frame ignored")
            continue
        if not isinstance(msg, dict):
            continue
        await on_json_message(msg)
