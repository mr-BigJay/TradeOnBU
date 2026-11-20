"""Socket heartbeats must not wait for SQLite flush or HTTP depth snapshots."""
import asyncio
import json
from collections import defaultdict

import pytest

from app.services.price_forecast import bitunix_trade_ws as ws


@pytest.mark.asyncio
async def test_slow_sqlite_and_depth_worker_do_not_starve_bitunix_pongs(monkeypatch):
    # Simulate the real-world ~60s persistence slot with a short test interval.
    # The job deliberately takes longer than the heartbeat deadline, but it
    # must not delay the independent socket receive task.
    monkeypatch.setattr(ws, "PERSIST_INTERVAL_SECONDS", 0.015)
    monkeypatch.setattr(ws, "APP_PING_INTERVAL_SECONDS", 0.03)
    monkeypatch.setattr(ws, "APP_PONG_TIMEOUT_SECONDS", 0.1)
    collector = ws.BitunixMicroDataCollector()
    collector._running = True
    slow_operations = []

    async def flush():
        slow_operations.append("flush")
        await asyncio.sleep(0.07)

    async def depth():
        slow_operations.append("depth")
        await asyncio.sleep(0.07)

    monkeypatch.setattr(collector, "_flush_trades", flush)
    monkeypatch.setattr(collector, "_snapshot_depth", depth)

    class FakeSocket:
        def __init__(self):
            self.pending = []
            self.pings = []

        async def send(self, raw):
            payload = json.loads(raw)
            if payload.get("op") == "ping":
                self.pings.append(payload["ping"])
                self.pending.append(json.dumps({
                    "op": "ping", "pong": payload["ping"]
                }))

        async def recv(self):
            if self.pending:
                return self.pending.pop(0)
            await asyncio.sleep(0.005)
            raise asyncio.TimeoutError()

    fake = FakeSocket()
    worker = asyncio.create_task(collector._persistence_loop())
    stream = asyncio.create_task(collector._stream_connected(fake))
    try:
        await asyncio.sleep(0.19)
        assert "flush" in slow_operations
        assert "depth" in slow_operations
        assert fake.pings
        assert collector.last_app_pong_at_ms is not None
        assert collector.last_app_pong_delay_ms is not None
        assert collector.heartbeat_timeout_count == 0
        assert not stream.done()
    finally:
        collector._running = False
        collector._persistence_wakeup.set()
        await asyncio.wait_for(stream, timeout=0.7)
        await asyncio.wait_for(worker, timeout=0.7)


@pytest.mark.asyncio
async def test_concurrent_flush_requests_cannot_double_count_trade_bucket(monkeypatch):
    collector = ws.BitunixMicroDataCollector()
    bucket_ms = 1_795_174_100_000
    collector._trade_buckets[bucket_ms] = {
        "buy_volume": 1.25, "sell_volume": 0.25, "trade_count": 3
    }
    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def commit(self):
            calls.append("commit")
            await asyncio.sleep(0.02)

        async def rollback(self):
            calls.append("rollback")

    async def record(_session, **payload):
        calls.append(("upsert", payload["trade_count"]))
        await asyncio.sleep(0.02)

    monkeypatch.setattr(ws, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(ws, "upsert_trade_bucket", record)
    await asyncio.gather(collector._flush_trades(), collector._flush_trades())
    assert calls == [("upsert", 3), "commit"]
    assert not collector._trade_buckets


@pytest.mark.asyncio
async def test_worker_early_wakeup_flushes_reconnect_without_depth_request(monkeypatch):
    monkeypatch.setattr(ws, "PERSIST_INTERVAL_SECONDS", 10)
    collector = ws.BitunixMicroDataCollector()
    collector._running = True
    events = []

    async def flush():
        events.append("flush")

    async def depth():
        events.append("depth")

    monkeypatch.setattr(collector, "_flush_trades", flush)
    monkeypatch.setattr(collector, "_snapshot_depth", depth)
    worker = asyncio.create_task(collector._persistence_loop())
    await asyncio.sleep(0)
    collector._persistence_wakeup.set()
    await asyncio.sleep(0.03)
    assert events == ["flush"]
    collector._running = False
    collector._persistence_wakeup.set()
    await asyncio.wait_for(worker, timeout=0.5)
    # A graceful shutdown writes any partial bucket one more time and
    # never fetches HTTP depth just because a reconnect happened.
    assert events == ["flush", "flush"]


@pytest.mark.asyncio
async def test_graceful_stop_waits_for_in_flight_persistence(monkeypatch):
    collector = ws.BitunixMicroDataCollector()
    started = asyncio.Event()
    complete = asyncio.Event()
    events = []

    async def idle_socket_worker():
        try:
            while collector._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            events.append("socket_cancelled")
            raise

    async def slow_flush():
        events.append("flush_start")
        started.set()
        await asyncio.sleep(0.035)
        events.append("flush_complete")
        complete.set()

    monkeypatch.setattr(collector, "_run_loop", idle_socket_worker)
    monkeypatch.setattr(collector, "_flush_trades", slow_flush)
    monkeypatch.setattr(collector, "_snapshot_depth", lambda: None)
    collector.start()
    try:
        collector._persistence_wakeup.set()
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await asyncio.wait_for(collector.stop(), timeout=1)
    finally:
        collector._running = False
    assert complete.is_set()
    assert "flush_complete" in events
    assert collector._persistence_task.done()
