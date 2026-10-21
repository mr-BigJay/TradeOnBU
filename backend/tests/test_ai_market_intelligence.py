"""Public snapshots enhance AI context without extra LLM calls or fabricated observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MarketDataSnapshot, MarketMicroRollup
from app.services.ai_market_intelligence import compact_snapshot, load_ai_market_intelligence

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def snapshot(*, payload, age_seconds=20, symbol="BTCUSDT", valid=True):
    return MarketDataSnapshot(
        symbol=symbol,
        valid=valid,
        collected_at=NOW - timedelta(seconds=age_seconds),
        payload={"symbol": symbol, **payload},
        freshness={},
    )


def test_compact_source_labels_and_no_future_liquidation_targets():
    liq = {
        "status": "partial",
        "bybit": {
            "status": "ok",
            "source_quality": "bybit_full_tape",
            "event_query_quality": {"truncated": False},
            "windows": {"1h": {
                "coverage": "complete",
                "partial": False,
                "metrics": {
                    "long_liquidation_notional_usd": 120000,
                    "short_liquidation_notional_usd": 40000,
                },
            }},
        },
        "binance": {"status": "unavailable"},
    }
    row = snapshot(payload={
        "funding": {"status": "ok", "source": "bitunix", "funding_rate": 0.0001},
        "open_interest": {
            "status": "ok", "source": "binance_futures", "period": "1h",
            "points": [
                {"time_ms": NOW_MS - 3600000, "open_interest": 100},
                {"time_ms": NOW_MS - 60000, "open_interest": 110},
            ],
        },
        "trade_flow": {
            "source": "bitunix_ws_trades", "status": "ok",
            "interpretation": "provisional_volume_delta_not_verified_cvd",
            "buckets_5m": [
                {"bucket_start_ms": NOW_MS - 5 * 60000, "buy_volume": 12, "sell_volume": 10},
            ],
            "collector": {"freshness": "ok", "gap_detected": True},
        },
        "order_book": {"status": "ok", "source": "bitunix", "imbalance_ratio": 0.25},
        "liquidations": liq,
    })
    ctx = compact_snapshot(row, symbol="BTCUSDT", now=NOW)
    assert ctx["status"] == "available"
    sources = ctx["sources"]
    assert sources["funding"]["source"] == "bitunix"
    assert sources["open_interest"]["source"] == "binance_futures"
    assert sources["open_interest"]["change_from_previous_pct"] == 10.0
    assert sources["trade_flow"]["volume_delta"] == 2
    assert sources["trade_flow"]["interpretation"] == "provisional_side_delta_not_verified_cvd"
    assert sources["liquidations"]["not_market_wide_total"]
    assert sources["liquidations"]["sources"]["bybit"]["long_liquidated_usd_observed"] == 120000
    assert "binance" not in sources["liquidations"]["sources"]
    assert "price_clusters" not in str(ctx)


def test_reject_old_snapshot_invalid_values_and_old_orderbook():
    row = snapshot(payload={
        "funding": {"status": "ok", "source": "bitunix", "funding_rate": float("nan")},
        "open_interest": {"status": "unavailable"},
        "trade_flow": {"status": "stale"},
        "liquidations": {"status": "disabled"},
        "order_book": {"status": "ok", "source": "bitunix", "imbalance_ratio": 0.2},
    }, age_seconds=600)
    assert compact_snapshot(row, symbol="BTCUSDT", now=NOW)["status"] == "unavailable"
    row.payload["funding"] = {"status": "ok", "source": "bitunix", "funding_rate": 0.00008}
    ctx = compact_snapshot(row, symbol="BTCUSDT", now=NOW)
    assert "order_book" not in ctx["sources"]
    assert ctx["sources"]["funding"]["funding_rate"] == 0.00008
    assert compact_snapshot(row, symbol="ETHUSDT", now=NOW)["status"] == "unavailable"
    assert compact_snapshot(snapshot(payload={}, age_seconds=1800), symbol="BTCUSDT", now=NOW)["status"] == "unavailable"
    assert compact_snapshot(snapshot(payload={}, age_seconds=-30), symbol="BTCUSDT", now=NOW)["status"] == "unavailable"


def test_reject_stale_trade_rollup_and_truncated_liquidation_source():
    row = snapshot(payload={
        "funding": {"status": "ok", "source": "bitunix", "funding_rate": 0},
        "trade_flow": {
            "source": "bitunix_ws_trades", "status": "ok",
            "interpretation": "provisional_volume_delta_not_verified_cvd",
            "buckets_5m": [{"bucket_start_ms": NOW_MS - 3600000, "buy_volume": 1, "sell_volume": 2}],
            "collector": {"freshness": "ok"},
        },
        "liquidations": {
            "status": "ok",
            "bybit": {
                "status": "ok",
                "event_query_quality": {"truncated": True},
                "windows": {"1h": {"coverage": "partial", "metrics": {
                    "long_liquidation_notional_usd": 123, "short_liquidation_notional_usd": 456,
                }}},
            },
        },
    })
    ctx = compact_snapshot(row, symbol="BTCUSDT", now=NOW)
    assert list(ctx["sources"]) == ["funding"]


@pytest.mark.asyncio
async def test_load_latest_public_snapshot_read_only(session):
    from datetime import datetime, timezone
    current = datetime.now(timezone.utc)
    session.add(MarketDataSnapshot(
        symbol="BTCUSDT",
        valid=True,
        collected_at=current - timedelta(seconds=35),
        payload={"symbol": "BTCUSDT", "funding": {
            "source": "bitunix", "status": "ok", "funding_rate": 0.0002
        }},
        freshness={},
    ))
    session.add(MarketDataSnapshot(
        symbol="ETHUSDT",
        valid=True,
        collected_at=current,
        payload={"symbol": "ETHUSDT", "funding": {
            "source": "bitunix", "status": "ok", "funding_rate": 0.0008
        }},
        freshness={},
    ))
    await session.commit()
    result = await load_ai_market_intelligence(session, symbol="BTCUSDT")
    assert result["status"] == "available"
    assert result["sources"]["funding"]["funding_rate"] == 0.0002
    result = await load_ai_market_intelligence(session, symbol="XRPUSDT")
    assert result["status"] == "unavailable"


def test_ict_prompt_still_requires_all_structural_gates():
    from app.services.ai_ict_analysis import INSTRUCTIONS
    assert "market_intelligence" in INSTRUCTIONS
    for term in ("sweep", "POI", "MSS", "R:R"):
        assert term in INSTRUCTIONS
    assert "not verified CVD" in INSTRUCTIONS


@pytest.mark.asyncio
async def test_recent_micro_rollups_override_old_snapshot_trade_and_depth(session):
    """AI reads the one-minute persisted observations, not 15-minute-old book data."""
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    trade_bucket = (now_ms // 300000) * 300000
    depth_bucket = (now_ms // 60000) * 60000 - 60000
    session.add_all([
        MarketDataSnapshot(
            symbol="BTCUSDT", valid=True, collected_at=now - timedelta(minutes=12),
            payload={"symbol": "BTCUSDT",
                     "funding": {"source": "bitunix", "status": "ok", "funding_rate": 0.0001},
                     "trade_flow": {"status": "stale"},
                     "order_book": {"source": "bitunix", "status": "ok",
                                    "imbalance_ratio": -0.8, "levels_used": 15}},
            freshness={},
        ),
        MarketMicroRollup(
            symbol="BTCUSDT", kind="trade_5m", source="bitunix_ws_trades",
            bucket_start_ms=trade_bucket,
            payload={"buy_volume": 17.0, "sell_volume": 4.0, "trade_count": 20},
        ),
        MarketMicroRollup(
            symbol="BTCUSDT", kind="depth_1m", source="bitunix",
            bucket_start_ms=depth_bucket,
            payload={"imbalance_ratio": 0.35, "levels_used": 15},
        ),
    ])
    await session.commit()
    context = await load_ai_market_intelligence(session, symbol="BTCUSDT")
    assert context["status"] == "available"
    src = context["sources"]
    assert src["funding"]["funding_rate"] == 0.0001
    assert src["trade_flow"]["volume_delta"] == 13
    assert src["trade_flow"]["trade_count"] == 20
    assert src["trade_flow"]["bucket_partial"] is True
    assert src["trade_flow"]["interpretation"] == "provisional_side_delta_not_verified_cvd"
    assert src["order_book"]["top15_imbalance_ratio"] == 0.35
    assert src["order_book"]["age_seconds"] <= 180


@pytest.mark.asyncio
async def test_stale_microdata_is_dropped_instead_of_keeping_old_snapshot_trade(session):
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    session.add_all([
        MarketDataSnapshot(
            symbol="BTCUSDT", valid=True, collected_at=now - timedelta(minutes=10),
            payload={"symbol": "BTCUSDT",
                     "funding": {"source": "bitunix", "status": "ok", "funding_rate": 0.0002},
                     "trade_flow": {"source": "bitunix_ws_trades", "status": "ok",
                                    "interpretation": "provisional_volume_delta_not_verified_cvd",
                                    "buckets_5m": [{"bucket_start_ms": now_ms - 8*60000,
                                                     "buy_volume": 99, "sell_volume": 1}],
                                    "collector": {"freshness": "ok"}}},
            freshness={},
        ),
        MarketMicroRollup(
            symbol="BTCUSDT", kind="trade_5m", source="bitunix_ws_trades",
            bucket_start_ms=now_ms - 9 * 60000,
            payload={"buy_volume": 1, "sell_volume": 2, "trade_count": 3},
        ),
        MarketMicroRollup(
            symbol="BTCUSDT", kind="depth_1m", source="bitunix",
            bucket_start_ms=now_ms - 4 * 60000,
            payload={"imbalance_ratio": 0.25, "levels_used": 15},
        ),
    ])
    await session.commit()
    context = await load_ai_market_intelligence(session, symbol="BTCUSDT")
    assert context["status"] == "available"
    assert set(context["sources"]) == {"funding"}


@pytest.mark.asyncio
async def test_microdata_wrong_symbol_or_invalid_quality_not_included(session):
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    session.add_all([
        MarketDataSnapshot(
            symbol="BTCUSDT", valid=True, collected_at=now,
            payload={"symbol": "BTCUSDT",
                     "funding": {"source": "bitunix", "status": "ok", "funding_rate": 0.0001}},
            freshness={},
        ),
        MarketMicroRollup(
            symbol="ETHUSDT", kind="trade_5m", source="bitunix_ws_trades",
            bucket_start_ms=(now_ms // 300000) * 300000,
            payload={"buy_volume": 100, "sell_volume": 1, "trade_count": 100},
        ),
        MarketMicroRollup(
            symbol="BTCUSDT", kind="depth_1m", source="unknown",
            bucket_start_ms=(now_ms // 60000) * 60000,
            payload={"imbalance_ratio": 0.9, "levels_used": 15},
        ),
    ])
    await session.commit()
    context = await load_ai_market_intelligence(session, symbol="BTCUSDT")
    assert context["status"] == "available"
    assert set(context["sources"]) == {"funding"}


def test_persisted_microdata_rejects_bad_values_and_future_buckets():
    from app.services.ai_market_intelligence import _persisted_trade_flow, _persisted_order_book
    assert _persisted_trade_flow(MarketMicroRollup(
        source="bitunix_ws_trades", kind="trade_5m", bucket_start_ms=NOW_MS + 60000,
        payload={"buy_volume": 5, "sell_volume": 1, "trade_count": 4},
    ), now_ms=NOW_MS) is None
    assert _persisted_trade_flow(MarketMicroRollup(
        source="bitunix_ws_trades", kind="trade_5m", bucket_start_ms=NOW_MS - 60000,
        payload={"buy_volume": float("nan"), "sell_volume": 1, "trade_count": 4},
    ), now_ms=NOW_MS) is None
    assert _persisted_order_book(MarketMicroRollup(
        source="bitunix", kind="depth_1m", bucket_start_ms=NOW_MS - 10000,
        payload={"imbalance_ratio": 1.4, "levels_used": 15},
    ), now_ms=NOW_MS) is None
