from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import LiquidationEvent, LiquidationSourceStatus, LiquidationWindowRollup
from app.services.liquidation_intelligence.analytics import compute_price_clusters, compute_window_metrics, detect_burst
from app.services.liquidation_intelligence.collector import LiquidationIntelligenceCollector
from app.services.liquidation_intelligence.dedupe import build_dedupe_key
from app.services.liquidation_intelligence.coverage import events_in_aligned_bucket, window_connection_coverage
from app.services.liquidation_intelligence.health import connection_activity_state, window_data_coverage
from app.services.liquidation_intelligence.rollups import bucket_start_for_timestamp, materialize_rollups_for_symbol
from app.services.liquidation_intelligence.llm_compact import compact_liquidation_for_llm
from app.services.liquidation_intelligence.maintenance import run_liquidation_retention_tick
from app.services.liquidation_intelligence.parsers import (
    binance_side_to_liquidated_position,
    bybit_side_to_liquidated_position,
    parse_binance_force_order_message,
    parse_bybit_all_liquidation_message,
)
from app.services.liquidation_intelligence.repository import fetch_events_since, insert_liquidation_event, prune_liquidation_data, record_source_message
from app.services.liquidation_intelligence.rollups import bucket_start_for_timestamp
from app.services.liquidation_intelligence.summary import build_liquidation_forecast_summary
from app.services.liquidation_intelligence.types import NormalizedLiquidation
from app.services.liquidation_intelligence.ws_common import run_ws_message_loop
from app.services.price_forecast.market_collector import build_llm_market_summary
from app.models import MarketDataSnapshot


BYBIT_MSG = {
    "topic": "allLiquidation.BTCUSDT",
    "type": "snapshot",
    "ts": 1739502303204,
    "data": [{"T": 1739502302929, "s": "BTCUSDT", "S": "Buy", "v": "0.015", "p": "81000.5"}],
}

BINANCE_MSG = {
    "e": "forceOrder",
    "E": 1739502303000,
    "o": {
        "s": "BTCUSDT",
        "S": "SELL",
        "ps": "LONG",
        "q": "0.020",
        "z": "0.020",
        "l": "0.020",
        "ap": "80950.25",
        "p": "80950.0",
        "X": "FILLED",
        "i": 12345,
    },
}


def test_bybit_side_mapping():
    assert bybit_side_to_liquidated_position("Buy") == "long"
    assert bybit_side_to_liquidated_position("Sell") == "short"


def test_binance_side_mapping():
    assert binance_side_to_liquidated_position("SELL") == "long"
    assert binance_side_to_liquidated_position("BUY") == "short"


def test_parse_bybit_message():
    events = parse_bybit_all_liquidation_message(BYBIT_MSG)
    assert len(events) == 1
    ev = events[0]
    assert ev.liquidated_position_side == "long"
    assert ev.price_type == "bankruptcy_price"
    assert ev.quantity_btc == pytest.approx(0.015)


def test_parse_binance_message_ps_and_order_id():
    ev = parse_binance_force_order_message(BINANCE_MSG)
    assert ev is not None
    assert ev.liquidated_position_side == "long"
    assert ev.exchange_order_id == "12345"
    assert ev.price_type == "avg_fill_price"


def test_parse_binance_rejects_coin_m_symbol():
    msg = {
        "e": "forceOrder",
        "E": 1,
        "st": "COIN",
        "o": {"s": "BTCUSD_PERP", "S": "SELL", "z": "1", "ap": "1", "X": "FILLED", "i": 1},
    }
    assert parse_binance_force_order_message(msg) is None


def test_malformed_binance_returns_none_or_raises():
    assert parse_binance_force_order_message({"e": "trade"}) is None


def test_deduplication_key_stable():
    a = build_dedupe_key("bybit", "BTCUSDT", 1, "Buy", 100.0, 0.1)
    b = build_dedupe_key("bybit", "BTCUSDT", 1, "Buy", 100.0, 0.1)
    assert a == b


@pytest.mark.asyncio
async def test_insert_deduplication():
    await init_db()
    ev = parse_bybit_all_liquidation_message(BYBIT_MSG)[0]
    async with SessionLocal() as session:
        ins, upd = await insert_liquidation_event(session, ev)
        assert ins is True and upd is False
        ins2, upd2 = await insert_liquidation_event(session, ev)
        assert ins2 is False and upd2 is False
        await session.commit()
        count = (await session.execute(select(LiquidationEvent))).scalars().all()
        assert len(count) == 1


@pytest.mark.asyncio
async def test_binance_cumulative_snapshot_dedupe():
    await init_db()
    base = parse_binance_force_order_message(BINANCE_MSG)
    assert base is not None
    snap1 = NormalizedLiquidation(**{**base.__dict__, "quantity_btc": 0.01, "cumulative_qty": 0.01, "estimated_notional_usd": 800.0})
    snap2 = NormalizedLiquidation(
        **{
            **base.__dict__,
            "quantity_btc": 0.02,
            "cumulative_qty": 0.02,
            "estimated_notional_usd": 1600.0,
            "event_timestamp_ms": base.event_timestamp_ms + 100,
        }
    )
    async with SessionLocal() as session:
        assert (await insert_liquidation_event(session, snap1))[0] is True
        await session.commit()
        assert (await insert_liquidation_event(session, snap2))[1] is True
        await session.commit()
        rows = (await session.execute(select(LiquidationEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].quantity_btc == pytest.approx(0.02)


def test_price_clusters_not_mixed_by_price_type():
    now = int(time.time() * 1000)

    class E:
        pass

    bybit = E()
    bybit.source_exchange = "bybit"
    bybit.price_type = "bankruptcy_price"
    bybit.price = 80000.0
    bybit.liquidated_position_side = "long"
    bybit.estimated_notional_usd = 1000.0
    bybit.event_timestamp_ms = now

    binance = E()
    binance.source_exchange = "binance"
    binance.price_type = "avg_fill_price"
    binance.price = 80000.0
    binance.liquidated_position_side = "long"
    binance.estimated_notional_usd = 2000.0
    binance.event_timestamp_ms = now

    mixed = compute_price_clusters([bybit, binance], source_exchange=None)
    assert len(mixed) == 1
    assert mixed[0]["observed_notional_usd"] == pytest.approx(3000.0)

    bybit_only = compute_price_clusters([bybit, binance], source_exchange="bybit", price_type="bankruptcy_price")
    assert len(bybit_only) == 1
    assert bybit_only[0]["observed_notional_usd"] == pytest.approx(1000.0)
    assert bybit_only[0]["price_type"] == "bankruptcy_price"


def test_connection_idle_not_stale():
    now = int(time.time() * 1000)
    state = connection_activity_state(
        connected=True,
        last_connection_activity_at_ms=now - 1000,
        last_heartbeat_at_ms=now - 1000,
        last_event_at_ms=None,
        now_ms=now,
        stale_after_ms=300_000,
        subscription_confirmed=True,
    )
    assert state == "connected_idle"


def test_connection_stays_active_when_events_flow_past_stale_heartbeat():
    now = 10**12
    stale = 300_000
    state = connection_activity_state(
        connected=True,
        last_connection_activity_at_ms=now - 120_000,
        last_heartbeat_at_ms=now - 600_000,
        last_event_at_ms=now - 120_000,
        now_ms=now,
        stale_after_ms=stale,
        subscription_confirmed=True,
    )
    assert state == "connected_active"


def test_healthy_connection_zero_events_observed_none():
    now = 1_700_000_000_000
    st = LiquidationSourceStatus(
        source_exchange="bybit",
        connected=True,
        connected_since_ms=now - 6 * 60_000,
        subscription_confirmed=True,
        coverage_gaps=[],
    )
    cov = window_data_coverage(st, [], now, "5m")
    assert cov["coverage"] == "observed_none"
    assert cov["metrics"] is None


def test_disconnect_mid_window_marks_partial():
    now = 1_700_000_000_000
    window_ms = 5 * 60_000
    start = now - window_ms
    st = LiquidationSourceStatus(
        source_exchange="bybit",
        connected=True,
        connected_since_ms=now - 60_000,
        subscription_confirmed=True,
        coverage_gaps=[{"start_ms": start, "end_ms": now - 120_000}],
    )
    conn = window_connection_coverage(
        connected=st.connected,
        connected_since_ms=st.connected_since_ms,
        coverage_gaps=st.coverage_gaps,
        now_ms=now,
        window_start_ms=start,
        subscription_confirmed=True,
    )
    assert conn["coverage"] == "partial"


def test_restart_unknown_coverage_without_history():
    now = int(time.time() * 1000)
    st = LiquidationSourceStatus(source_exchange="binance", connected=True, subscription_confirmed=True)
    cov = window_data_coverage(st, [], now, "5m")
    assert cov["coverage"] == "unknown"
    assert cov["metrics"] is None


def test_time_aligned_bucket_boundaries():
    window_ms = 60_000
    bucket = bucket_start_for_timestamp(125_000, window_ms)
    assert bucket == 120_000

    class E:
        pass

    e = E()
    e.event_timestamp_ms = 125_000
    assert len(events_in_aligned_bucket([e], 120_000, 180_000)) == 1
    assert len(events_in_aligned_bucket([e], 60_000, 120_000)) == 0


@pytest.mark.asyncio
async def test_ws_recv_idle_calls_on_idle_not_disconnect():
    idle_calls = 0
    msg_calls = 0

    async def on_idle():
        nonlocal idle_calls
        idle_calls += 1

    async def on_msg(_msg):
        nonlocal msg_calls
        msg_calls += 1

    seq = iter([asyncio.TimeoutError(), '{"ok": true}'])

    async def fake_wait_for(_aw, _timeout=None, **_kwargs):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    ws = AsyncMock()
    with patch("app.services.liquidation_intelligence.ws_common.asyncio.wait_for", side_effect=fake_wait_for):
        await run_ws_message_loop(
            ws,
            on_json_message=on_msg,
            on_idle=on_idle,
            recv_idle_seconds=1.0,
            running=lambda: msg_calls < 1,
        )
    assert idle_calls >= 1
    assert msg_calls == 1


def test_collector_binance_url_default_market_path():
    c = LiquidationIntelligenceCollector()
    assert "/market/ws/" in c._binance_ws_url()


@pytest.mark.asyncio
async def test_rollup_persistence_and_idempotent():
    await init_db()
    now = int(time.time() * 1000)
    ev = parse_bybit_all_liquidation_message(BYBIT_MSG)[0]
    ev = NormalizedLiquidation(**{**ev.__dict__, "event_timestamp_ms": now - 60_000})
    async with SessionLocal() as session:
        await insert_liquidation_event(session, ev)
        await session.commit()
        n1 = await materialize_rollups_for_symbol(session, now_ms=now)
        n2 = await materialize_rollups_for_symbol(session, now_ms=now)
        await session.commit()
        assert n1 > 0
        assert n2 == n1
        rows = (await session.execute(select(LiquidationWindowRollup))).scalars().all()
        assert len(rows) >= 5
        names = {r.window_name for r in rows}
        assert "1m" in names and "4h" in names
        one_m = next(r for r in rows if r.window_name == "1m")
        assert one_m.payload.get("bucket_end_ms") == one_m.bucket_start_ms + 60_000
        assert one_m.payload.get("time_bucket_closed") in (True, False)
        assert one_m.payload["bucket_start_ms"] <= ev.event_timestamp_ms < one_m.payload["bucket_end_ms"]


@pytest.mark.asyncio
async def test_hourly_maintenance_materializes_all_window_sizes():
    await init_db()
    now = int(time.time() * 1000)
    ev = parse_bybit_all_liquidation_message(BYBIT_MSG)[0]
    ev = NormalizedLiquidation(**{**ev.__dict__, "event_timestamp_ms": now - 30_000})
    async with SessionLocal() as session:
        await insert_liquidation_event(session, ev)
        await session.commit()
    deleted = await run_liquidation_retention_tick()
    assert deleted[2] > 0


@pytest.mark.asyncio
async def test_retention_prunes_old_rows():
    await init_db()
    old_ms = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp() * 1000)
    ev = parse_bybit_all_liquidation_message(BYBIT_MSG)[0]
    ev = NormalizedLiquidation(
        **{**ev.__dict__, "event_timestamp_ms": old_ms, "dedupe_key": build_dedupe_key("bybit", "BTCUSDT", old_ms, "Buy", 1, 1)},
    )
    async with SessionLocal() as session:
        await insert_liquidation_event(session, ev)
        await session.commit()
        deleted = await prune_liquidation_data(session)
        await session.commit()
        assert deleted[0] >= 1


@pytest.mark.asyncio
async def test_summary_partial_when_disconnected():
    await init_db()
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True):
        async with SessionLocal() as session:
            await record_source_message(session, "bybit", connected=False, error="down")
            await session.commit()
            out = await build_liquidation_forecast_summary(session)
    assert out["status"] in ("unavailable", "partial")
    assert "freshness_and_coverage" in out
    assert isinstance(out["price_clusters"], dict)


def test_compact_for_llm_includes_per_source_metrics():
    full = {
        "status": "partial",
        "symbol": "BTCUSDT",
        "bybit": {
            "status": "ok",
            "source_quality": "bybit_full_tape",
            "connection_state": "connected_active",
            "event_freshness": "ok",
            "windows": {
                "5m": {
                    "coverage": "full",
                    "metrics": {"total_observed_notional_usd": 1500.0, "event_count": 2, "long_liquidation_notional_usd": 1500.0},
                }
            },
            "burst_metrics": {"burst": False},
        },
        "binance": {
            "status": "partial",
            "windows": {"5m": {"coverage": "unknown", "metrics": None}},
            "burst_metrics": {},
        },
        "combined_labeled": {"status": "partial_labeled", "windows": {}},
        "freshness_and_coverage": {},
        "price_clusters": {"bybit": list(range(10)), "binance": list(range(10))},
    }
    slim = compact_liquidation_for_llm(full)
    assert slim["bybit"]["windows"]["5m"]["metrics"]["total_observed_notional_usd"] == 1500.0
    assert slim["binance"]["windows"]["5m"]["metrics"] is None
    assert len(slim.get("price_clusters", {}).get("bybit", [])) <= 3


@pytest.mark.asyncio
async def test_gpt_summary_includes_connection_coverage():
    await init_db()
    now = int(time.time() * 1000)
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True):
        async with SessionLocal() as session:
            await record_source_message(
                session,
                "bybit",
                connected=True,
                connection_activity_at_ms=now,
                subscription_confirmed=True,
            )
            await session.commit()
            full = await build_liquidation_forecast_summary(session)
    slim = compact_liquidation_for_llm(full)
    assert "freshness_and_coverage" in slim
    assert full["bybit"]["windows"]["5m"]["coverage"] in ("unknown", "observed_none", "partial", "full", "empty")
    assert "bybit" in slim and "binance" in slim


@pytest.mark.asyncio
async def test_market_snapshot_liquidation_metrics_reach_llm_json():
    await init_db()
    now = int(time.time() * 1000)
    bybit_ev = parse_bybit_all_liquidation_message(BYBIT_MSG)[0]
    bybit_ev = NormalizedLiquidation(
        **{
            **bybit_ev.__dict__,
            "event_timestamp_ms": now - 120_000,
            "estimated_notional_usd": 1215.0,
            "dedupe_key": build_dedupe_key("bybit", "BTCUSDT", now - 120_000, "Buy", 81000.5, 0.015),
        }
    )
    binance_ev = parse_binance_force_order_message(BINANCE_MSG)
    assert binance_ev is not None
    binance_ev = NormalizedLiquidation(
        **{
            **binance_ev.__dict__,
            "event_timestamp_ms": now - 90_000,
            "estimated_notional_usd": 1619.0,
        }
    )
    conn_since = now - 4 * 60 * 60 * 1000
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True):
        async with SessionLocal() as session:
            await insert_liquidation_event(session, bybit_ev)
            await insert_liquidation_event(session, binance_ev)
            await record_source_message(
                session,
                "bybit",
                connected=True,
                connection_activity_at_ms=now,
                subscription_confirmed=True,
            )
            await record_source_message(
                session,
                "binance",
                connected=True,
                connection_activity_at_ms=now,
                subscription_confirmed=True,
            )
            st_b = (
                await session.execute(select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == "bybit"))
            ).scalar_one()
            st_n = (
                await session.execute(select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == "binance"))
            ).scalar_one()
            st_b.connected_since_ms = conn_since
            st_b.coverage_gaps = [{"start_ms": conn_since, "end_ms": now}]
            st_n.connected_since_ms = conn_since
            st_n.coverage_gaps = [{"start_ms": conn_since, "end_ms": now}]
            await session.commit()

            from app.services.liquidation_intelligence.summary import build_liquidation_forecast_summary
            from app.services.price_forecast.market_collector import build_llm_market_summary

            full = await build_liquidation_forecast_summary(session)
            slim = compact_liquidation_for_llm(full)
            snap = MarketDataSnapshot(
                symbol="BTCUSDT",
                payload={"collected_at": "t", "liquidations": slim},
                freshness={"liquidations": slim.get("status")},
            )
            llm_json = build_llm_market_summary(snap, anchor_price=80000.0, expected_times_iso=["2026-01-01T12:00:00Z"])
            import json

            parsed = json.loads(llm_json)
            liq = parsed["liquidations"]
            bybit_5m = liq["bybit"]["windows"]["5m"]
            assert bybit_5m["metrics"] is not None
            assert bybit_5m["metrics"]["total_observed_notional_usd"] == pytest.approx(1215.0, rel=0.01)
            binance_5m = liq["binance"]["windows"]["5m"]
            assert binance_5m["metrics"] is not None
            assert binance_5m["metrics"]["total_observed_notional_usd"] == pytest.approx(1619.0, rel=0.01)
            combined_5m = liq["combined_labeled"]["windows"]["5m"]
            assert combined_5m["metrics"]["total_observed_notional_usd"] == pytest.approx(2834.0, rel=0.01)
            assert set(combined_5m["contributing_sources"]) == {"bybit", "binance"}


@pytest.mark.asyncio
async def test_subscription_reset_on_disconnect_and_pending_on_connect():
    await init_db()
    async with SessionLocal() as session:
        await record_source_message(session, "bybit", connected=True, subscription_confirmed=True)
        await record_source_message(session, "bybit", connected=False)
        await session.commit()
        st = (await session.execute(select(LiquidationSourceStatus))).scalar_one()
        assert st.subscription_confirmed is False
        await record_source_message(session, "bybit", connected=True)
        await session.commit()
        st2 = (await session.execute(select(LiquidationSourceStatus))).scalar_one()
        assert st2.connected is True
        assert st2.subscription_confirmed is False


@pytest.mark.asyncio
async def test_startup_reconcile_unclean_connected_flag():
    await init_db()
    now = int(time.time() * 1000)
    async with SessionLocal() as session:
        await record_source_message(
            session,
            "bybit",
            connected=True,
            connection_activity_at_ms=now - 60_000,
            subscription_confirmed=True,
        )
        st = (await session.execute(select(LiquidationSourceStatus))).scalar_one()
        st.connected_since_ms = now - 3600_000
        await session.commit()
        from app.services.liquidation_intelligence.startup_reconcile import reconcile_liquidation_sources_on_startup

        n = await reconcile_liquidation_sources_on_startup(session)
        await session.commit()
        st2 = (await session.execute(select(LiquidationSourceStatus))).scalar_one()
        assert n == 1
        assert st2.connected is False
        assert st2.subscription_confirmed is False
        assert st2.coverage_gaps and st2.coverage_gaps[0]["end_ms"] == now - 60_000


@pytest.mark.asyncio
async def test_rollup_large_dataset_exact_totals():
    await init_db()
    window_ms = 60_000
    bucket = bucket_start_for_timestamp(1_700_000_100_000, window_ms)
    now = bucket + window_ms + 5000
    ts = bucket + 30_000
    expected = 0.0
    async with SessionLocal() as session:
        for i in range(800):
            notional = 10.0 + (i % 7)
            expected += notional
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts + (i % 50),
                received_timestamp_ms=now,
                liquidated_position_side="long" if i % 2 == 0 else "short",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.001,
                estimated_notional_usd=notional,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts + i, "Buy", 80000.0, i),
            )
            await insert_liquidation_event(session, ev)
        await session.commit()
        n1 = await materialize_rollups_for_symbol(session, now_ms=now)
        n2 = await materialize_rollups_for_symbol(session, now_ms=now)
        await session.commit()
        assert n2 == n1
        row = (
            await session.execute(
                select(LiquidationWindowRollup).where(
                    LiquidationWindowRollup.window_name == "1m",
                    LiquidationWindowRollup.source_exchange == "bybit",
                    LiquidationWindowRollup.bucket_start_ms == bucket,
                )
            )
        ).scalar_one()
        assert row.payload["total_observed_notional_usd"] == pytest.approx(expected, rel=0.001)
        assert row.payload["event_count"] == 800


def test_llm_market_summary_includes_liquidations_key():
    snap = MarketDataSnapshot(
        symbol="BTCUSDT",
        payload={"collected_at": "t", "liquidations": {"status": "disabled"}},
        freshness={"liquidations": "disabled"},
    )
    out = build_llm_market_summary(snap, anchor_price=80000.0, expected_times_iso=["2026-01-01T12:00:00Z"])
    assert "liquidations" in out


@pytest.mark.asyncio
async def test_ict_instructions_untouched():
    from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS

    assert "Liquidation Intelligence" not in ICT_TRADER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_record_event_bumps_connection_activity():
    await init_db()
    now = int(time.time() * 1000)
    async with SessionLocal() as session:
        await record_source_message(session, "bybit", connected=True, connection_activity_at_ms=now - 500_000)
        await record_source_message(
            session,
            "bybit",
            connected=True,
            message_at_ms=now,
            connection_activity_at_ms=now,
            received_delta=1,
        )
        await session.commit()
        st = (await session.execute(select(LiquidationSourceStatus))).scalar_one()
        assert st.last_connection_activity_at_ms == now
        assert st.last_event_at_ms == now


@pytest.mark.asyncio
async def test_independent_source_failures():
    await init_db()
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True):
        async with SessionLocal() as session:
            await record_source_message(
                session,
                "bybit",
                connected=True,
                connection_activity_at_ms=int(time.time() * 1000),
                subscription_confirmed=True,
            )
            await record_source_message(session, "binance", connected=False, error="ws_closed")
            await session.commit()
            out = await build_liquidation_forecast_summary(session)
    assert out["bybit"]["connection_state"] in ("connected_idle", "connecting", "stale", "connected_active")
    assert out["binance"]["connection_state"] == "disconnected"


def test_aggregation_windows():
    now = int(time.time() * 1000)

    class E:
        pass

    events = []
    for i in range(3):
        e = E()
        e.event_timestamp_ms = now - i * 60_000
        e.liquidated_position_side = "long"
        e.estimated_notional_usd = 800.0
        e.price = 80000.0
        events.append(e)

    metrics = compute_window_metrics(events, now)
    assert metrics["5m"]["event_count"] == 3


def test_burst_detection():
    b = detect_burst(5000, 1000)
    assert b["burst"] is True


@pytest.mark.asyncio
async def test_fetch_not_truncated_when_count_below_limit():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 60_000
    async with SessionLocal() as session:
        for i in range(2):
            ts = since + i * 1000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=1.0,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, float(i)),
            )
            await insert_liquidation_event(session, ev)
        await session.commit()
        result = await fetch_events_since(session, symbol="BTCUSDT", since_ms=since, source="bybit", limit=5)
        assert result.truncated is False
        assert result.rows_returned == 2


@pytest.mark.asyncio
async def test_fetch_not_truncated_when_count_equals_limit():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 60_000
    async with SessionLocal() as session:
        for i in range(3):
            ts = since + i * 1000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=1.0,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, float(i)),
            )
            await insert_liquidation_event(session, ev)
        await session.commit()
        result = await fetch_events_since(session, symbol="BTCUSDT", since_ms=since, source="bybit", limit=3)
        assert result.truncated is False
        assert result.rows_returned == 3
        assert result.matching_row_count is None


@pytest.mark.asyncio
async def test_fetch_truncation_detected_with_small_limit():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 4 * 60 * 60 * 1000
    async with SessionLocal() as session:
        for i in range(10):
            ts = since + (i + 1) * 60_000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=100.0 + i,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, i),
            )
            await insert_liquidation_event(session, ev)
        await session.commit()
        result = await fetch_events_since(session, symbol="BTCUSDT", since_ms=since, source="bybit", limit=3)
        assert result.truncated is True
        assert result.rows_returned == 3
        assert result.matching_row_count == 10


@pytest.mark.asyncio
async def test_truncation_preserves_5m_suppresses_4h_derived():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 4 * 60 * 60 * 1000
    async with SessionLocal() as session:
        for i in range(12):
            ts = since + (i + 1) * 120_000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=50.0 + i,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, float(i)),
            )
            await insert_liquidation_event(session, ev)
        for i in range(5):
            ts = now - 320_000 + i * 40_000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=100.0 + i,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, 100.0 + i),
            )
            await insert_liquidation_event(session, ev)
        await record_source_message(
            session,
            "bybit",
            connected=True,
            connection_activity_at_ms=now,
            subscription_confirmed=True,
        )
        st = (await session.execute(select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == "bybit"))).scalar_one()
        st.connected_since_ms = since
        st.coverage_gaps = [{"start_ms": since, "end_ms": now}]
        await session.commit()
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True), patch.object(
        get_settings(), "liquidation_forecast_fetch_limit", 5
    ):
        async with SessionLocal() as session:
            full = await build_liquidation_forecast_summary(session)
    slim = compact_liquidation_for_llm(full)
    w5 = full["bybit"]["windows"]["5m"]
    w4 = full["bybit"]["windows"]["4h"]
    assert w4["coverage"] == "truncated"
    assert w5["coverage"] != "truncated"
    assert w5["metrics"] is not None
    assert w5["metrics"]["event_count"] >= 4
    assert full["bybit"]["windows"]["15m"]["coverage"] == "truncated"
    assert full["bybit"]["derived_analytics_quality"]["burst_metrics"] == "suppressed"


@pytest.mark.asyncio
async def test_truncation_suppresses_burst_and_clusters_when_inputs_affected():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 4 * 60 * 60 * 1000
    async with SessionLocal() as session:
        for i in range(15):
            ts = now - (14 - i) * 60_000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=200.0,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, float(i)),
            )
            await insert_liquidation_event(session, ev)
        await record_source_message(
            session,
            "bybit",
            connected=True,
            connection_activity_at_ms=now,
            subscription_confirmed=True,
        )
        st = (await session.execute(select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == "bybit"))).scalar_one()
        st.connected_since_ms = since
        st.coverage_gaps = [{"start_ms": since, "end_ms": now}]
        await session.commit()
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True), patch.object(
        get_settings(), "liquidation_forecast_fetch_limit", 3
    ):
        async with SessionLocal() as session:
            full = await build_liquidation_forecast_summary(session)
    slim = compact_liquidation_for_llm(full)
    assert full["bybit"]["windows"]["15m"]["coverage"] == "truncated"
    assert full["bybit"]["windows"]["1h"]["coverage"] == "truncated"
    assert full["bybit"]["derived_analytics_quality"]["burst_metrics"] == "suppressed"
    assert slim["bybit"]["burst_metrics"].get("unreliable") is True
    assert full["bybit"]["derived_analytics_quality"]["price_clusters"] == "suppressed"
    assert slim["bybit"]["price_clusters"] == []


@pytest.mark.asyncio
async def test_truncation_marks_summary_and_gpt_compact():
    await init_db()
    now = int(time.time() * 1000)
    since = now - 4 * 60 * 60 * 1000
    conn_since = since
    async with SessionLocal() as session:
        for i in range(10):
            ts = since + (i + 1) * 60_000
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=ts,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=200.0,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", ts, "Buy", 80000.0, float(i)),
            )
            await insert_liquidation_event(session, ev)
        await record_source_message(
            session,
            "bybit",
            connected=True,
            connection_activity_at_ms=now,
            subscription_confirmed=True,
        )
        st = (await session.execute(select(LiquidationSourceStatus).where(LiquidationSourceStatus.source_exchange == "bybit"))).scalar_one()
        st.connected_since_ms = conn_since
        st.coverage_gaps = [{"start_ms": conn_since, "end_ms": now}]
        await session.commit()
    with patch.object(get_settings(), "liquidation_intelligence_enabled", True), patch.object(
        get_settings(), "liquidation_forecast_fetch_limit", 3
    ):
        async with SessionLocal() as session:
            full = await build_liquidation_forecast_summary(session)
    slim = compact_liquidation_for_llm(full)
    assert full["event_query_quality"]["bybit"]["truncated"] is True
    assert full["bybit"]["windows"]["4h"]["coverage"] == "truncated"
    assert full["bybit"]["windows"]["4h"]["metrics"] is None
    assert slim["event_query_quality"]["bybit"]["warning"]
    assert slim["bybit"]["windows"]["4h"]["metrics"] is None


@pytest.mark.asyncio
async def test_closed_empty_rollup_between_populated_buckets():
    await init_db()
    window_ms = 60_000
    b0 = bucket_start_for_timestamp(1_700_000_000_000, window_ms)
    b1 = b0 + window_ms
    b2 = b0 + 2 * window_ms
    now_ms = b2 + window_ms + 5000
    async with SessionLocal() as session:
        for bucket, idx in ((b0, 1), (b2, 2)):
            ev = NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=bucket + 10_000,
                received_timestamp_ms=now_ms,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=500.0 * idx,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", bucket, "Buy", 80000.0, idx),
            )
            await insert_liquidation_event(session, ev)
        await session.commit()
        await materialize_rollups_for_symbol(session, now_ms=now_ms, sources=("bybit",))
        await session.commit()
        mid = (
            await session.execute(
                select(LiquidationWindowRollup).where(
                    LiquidationWindowRollup.window_name == "1m",
                    LiquidationWindowRollup.source_exchange == "bybit",
                    LiquidationWindowRollup.bucket_start_ms == b1,
                )
            )
        ).scalar_one_or_none()
        assert mid is not None
        assert mid.payload["observation"] == "no_events_observed"
        assert mid.payload["time_bucket_closed"] is True
        assert mid.payload["event_count"] == 0
