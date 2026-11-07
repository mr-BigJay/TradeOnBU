"""Lossless analyst-input transport and per-section size accounting.

Only the API request is packed. Raw candle dictionaries remain available for charting,
audit and the independent paper monitor. No candles or volume observations are dropped.
"""
from copy import deepcopy
import json

OHLCV_COLUMNS = ("time_ms", "open", "high", "low", "close", "volume")


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def pack_market(market):
    packed = deepcopy(market)
    for tf, frame in packed.get("timeframes", {}).items():
        candles = frame.get("candles")
        if not isinstance(candles, list):
            continue
        if not candles or not all(isinstance(row, dict) for row in candles):
            continue
        # Preserve every OHLCV sample and the timestamp; time_iso is derivable
        # exactly from time_ms and would otherwise be repeated for every row.
        if not all(all(k in row for k in OHLCV_COLUMNS[:5]) for row in candles):
            continue
        frame["candles"] = [[row.get(k) for k in OHLCV_COLUMNS] for row in candles]
        frame["candles_columns"] = list(OHLCV_COLUMNS)
        frame["candles_format"] = "arrays_utc_epoch_ms"
    return packed


def build_analyst_payload(*, market, evidence_catalog, market_intelligence,
                          strategy_features, enabled_strategies, execution_regime):
    before = {
        "market": market, "evidence_catalog": evidence_catalog,
        "market_intelligence": market_intelligence, "strategy_features": strategy_features,
        "enabled_strategies": enabled_strategies, "execution_regime": execution_regime,
    }
    after = {**before, "market": pack_market(market)}
    section_sizes = {
        key: {"before_chars": _size(value), "after_chars": _size(after[key])}
        for key, value in before.items()
    }
    base = _size(before)
    compact = _size(after)
    audit = {
        "version": "analyst_payload_ohlcv_v1",
        "before_chars": base, "after_chars": compact,
        "saved_chars": base - compact,
        "reduction_pct": round(100 * (base - compact) / base, 2) if base else 0,
        "sections": section_sizes,
        "candle_counts": {tf: len(frame.get("candles") or [])
                          for tf, frame in market.get("timeframes", {}).items()},
        "policy": "lossless OHLCV/time_ms, omit derivable per-candle time_iso; no candle trimming",
    }
    return json.dumps(after, ensure_ascii=False, separators=(",", ":")), audit
