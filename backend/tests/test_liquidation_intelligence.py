from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import LiquidationEvent, LiquidationSourceStatus
from app.services.liquidation_intelligence.analytics import compute_window_metrics, detect_burst
from app.services.liquidation_intelligence.dedupe import build_dedupe_key
from app.services.liquidation_intelligence.llm_compact import compact_liquidation_for_llm
from app.services.liquidation_intelligence.parsers import (
    binance_side_to_liquidated_position,
    bybit_side_to_liquidated_position,
    parse_binance_force_order_message,
    parse_bybit_all_liquidation_message,
)
from app.services.liquidation_intelligence.repository import insert_liquidation_event, prune_liquidation_data
from app.services.liquidation_intelligence.types import NormalizedLiquidation
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
    assert ev.estimated_notional_usd == pytest.approx(81000.5 * 0.015)


def test_parse_binance_message():
    ev = parse_binance_force_order_message(BINANCE_MSG)
    assert ev is not None
    assert ev.liquidated_position_side == "long"
    assert ev.quantity_btc == pytest.approx(0.02)
    assert ev.source_quality == "sampled_1000ms_window"


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
        assert await insert_liquidation_event(session, ev) is True
        assert await insert_liquidation_event(session, ev) is False
        await session.commit()
        count = (await session.execute(select(LiquidationEvent))).scalars().all()
        assert len(count) == 1


@pytest.mark.asyncio
async def test_aggregation_windows():
    await init_db()
    now = int(time.time() * 1000)
    rows = []
    for i in range(3):
        rows.append(
            NormalizedLiquidation(
                source_exchange="bybit",
                symbol="BTCUSDT",
                event_timestamp_ms=now - i * 60_000,
                received_timestamp_ms=now,
                liquidated_position_side="long",
                native_order_side="Buy",
                price=80000.0,
                quantity_btc=0.01,
                estimated_notional_usd=800.0,
                price_type="bankruptcy_price",
                source_quality="observed",
                dedupe_key=build_dedupe_key("bybit", "BTCUSDT", now - i * 60_000, "Buy", 80000.0, 0.01),
            )
        )

    class E:
        pass

    events = []
    for r in rows:
        e = E()
        e.event_timestamp_ms = r.event_timestamp_ms
        e.liquidated_position_side = r.liquidated_position_side
        e.estimated_notional_usd = r.estimated_notional_usd
        e.price = r.price
        events.append(e)

    metrics = compute_window_metrics(events, now)
    assert metrics["5m"]["event_count"] == 3
    assert metrics["5m"]["long_liquidation_notional_usd"] == pytest.approx(2400.0)


def test_burst_detection():
    b = detect_burst(5000, 1000)
    assert b["burst"] is True
    assert b["intensity_vs_baseline"] == pytest.approx(5.0)


def test_compact_for_llm_removes_heavy_fields():
    full = {"status": "ok", "symbol": "BTCUSDT", "windows": {"5m": {"event_count": 1}}, "price_clusters": list(range(10))}
    slim = compact_liquidation_for_llm(full)
    assert len(slim.get("price_clusters", [])) <= 5


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
