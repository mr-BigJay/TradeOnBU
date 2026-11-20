"""Regression for Bitunix public trade WebSocket heartbeat and reconnects.

No real network, exchange authentication, or order placement.
"""
import asyncio
import json
import time

import pytest

from app.services.price_forecast import bitunix_trade_ws as ws


def test_bitunix_heartbeat_payload_and_ack_are_distinct_from_trade_messages():
    stamp = 1732519687
    assert json.loads(ws.heartbeat_ping_payload(stamp)) == {
        "op": "ping", "ping": stamp,
    }
    assert ws.matching_heartbeat_pong(
        {"op": "ping", "pong": stamp, "ping": stamp + 1}, stamp
    )
    assert not ws.matching_heartbeat_pong(
        {"op": "ping", "pong": stamp - 1}, stamp
    )
    assert not ws.matching_heartbeat_pong(
        {"ch": "trade", "pong": stamp}, stamp
    )
    assert not ws.matching_heartbeat_pong(
        {"op": "ping", "pong": stamp}, None
    )


@pytest.mark.asyncio
async def test_public_trade_stream_uses_json_ping_receives_pong_and_keeps_trades(monkeypatch):
    monkeypatch.setattr(ws, "APP_PING_INTERVAL_SECONDS", .01)
    monkeypatch.setattr(ws, "APP_PONG_TIMEOUT_SECONDS", .1)
    collector = ws.BitunixMicroDataCollector()
    collector._running = True
    class FakeStream:
        def __init__(self):
            self.sent = []
            self.calls = 0
            self.ping = None

        async def send(self, raw):
            msg = json.loads(raw)
            self.sent.append(msg)
            if msg.get("op") == "ping":
                self.ping = msg["ping"]

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(.02)
                raise asyncio.TimeoutError()
            if self.calls == 2:
                assert self.ping is not None
                return json.dumps({"op": "ping", "pong": self.ping, "ping": self.ping + 1})
            collector._running = False
            return json.dumps({
                "ch": "trade", "symbol": "BTCUSDT",
                "data": [{"s": "buy", "v": "0.9", "t": "2026-11-20T10:50:00Z"}]
            })
    stream = FakeStream()
    try:
        await collector._stream_connected(stream)
    finally:
        collector._running = False
    assert stream.sent[0] == {
        "op": "subscribe", "args": [{"symbol": "BTCUSDT", "ch": "trade"}]
    }
    assert stream.sent[1] == {
        "op": "ping", "ping": stream.ping
    }
    assert collector.last_app_ping_at_ms is not None
    assert collector.last_app_pong_at_ms is not None
    assert len(collector._trade_buckets) == 1
    b = next(iter(collector._trade_buckets.values()))
    assert b["buy_volume"] == .9
    assert b["sell_volume"] == 0
    assert b["trade_count"] == 1


@pytest.mark.asyncio
async def test_unanswered_json_pong_times_out_instead_of_marking_stream_healthy(monkeypatch):
    monkeypatch.setattr(ws, "APP_PING_INTERVAL_SECONDS", .005)
    monkeypatch.setattr(ws, "APP_PONG_TIMEOUT_SECONDS", .02)
    collector = ws.BitunixMicroDataCollector()
    collector._running = True

    class NoPong:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            await asyncio.sleep(.01)
            raise asyncio.TimeoutError()

    stream = NoPong()
    with pytest.raises(TimeoutError, match="bitunix_app_pong_timeout"):
        await asyncio.wait_for(collector._stream_connected(stream), timeout=1)
    assert collector.heartbeat_timeout_count == 1
    assert any(x.get("op") == "ping" for x in stream.sent)
    assert collector.last_app_pong_at_ms is None


@pytest.mark.asyncio
async def test_disconnect_flushes_partial_bucket_before_next_connection(monkeypatch):
    collector = ws.BitunixMicroDataCollector()
    collector._running = True
    events = []
    original_connect = ws.websockets.connect
    class Connection:
        async def __aenter__(self):
            events.append("connected")
            return self
        async def __aexit__(self, *_):
            events.append("disconnected")
        async def send(self, payload):
            events.append(json.loads(payload)["op"])
        async def recv(self):
            collector._ingest_trade({
                "s": "sell", "v": "0.4", "t": "2026-11-20T10:50:00Z"
            })
            collector._running = False
            raise RuntimeError("synthetic_disconnect")

    async def flush():
        events.append("flushed")
        collector._trade_buckets.clear()
    monkeypatch.setattr(ws.websockets, "connect", lambda *_a, **_kw: Connection())
    monkeypatch.setattr(collector, "_flush_trades", flush)
    try:
        await collector._run_loop()
    finally:
        monkeypatch.setattr(ws.websockets, "connect", original_connect)
    assert events == ["connected", "subscribe", "disconnected", "flushed"]
    assert collector.disconnect_count == 1
    assert collector.gap_detected
    assert collector.status_payload()["last_disconnect_reason"] == "synthetic_disconnect"
    assert collector.status_payload()["pending_buckets"] == 0


def test_status_never_promotes_stale_trade_feed_from_pong_alone():
    collector = ws.BitunixMicroDataCollector()
    collector.ws_connected = True
    collector.last_app_pong_at_ms = int(time.time() * 1000)
    assert collector.status_payload()["freshness"] == "unavailable"
    collector.last_trade_at_ms = int(time.time() * 1000) - ws.STALE_AFTER_MS * 2
    assert collector.status_payload()["freshness"] == "stale"
