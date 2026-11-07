"""Measured, relevance-aware analyst request transport.

All original candles stay available to the Paper monitor and chart. Only the
analyst's 1m micro-timing window is shortened (5m/15m/1h/4h stay complete).
Character/byte measurements are NOT per-section token counts: the API alone
reports exact aggregate tokens.
"""
from copy import deepcopy
from collections import Counter
import json

OHLCV_COLUMNS = ("time_ms", "open", "high", "low", "close", "volume")
# Forward 4h scenarios need the long HTF/LTF history; for event ordering the
# 1m bars are only supplementary to the 5m monitor's actual evidence.
ANALYST_1M_CLOSED_WINDOW = 45


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(value):
    return len(_json(value))


def _bytes(value):
    return len(_json(value).encode("utf-8"))


def pack_market(market):
    packed = deepcopy(market)
    for tf, frame in packed.get("timeframes", {}).items():
        candles = frame.get("candles")
        if not isinstance(candles, list) or not candles:
            continue
        if not all(isinstance(row, dict) and all(k in row for k in OHLCV_COLUMNS[:5])
                   for row in candles):
            continue
        original_count = len(candles)
        if tf == "1m" and len(candles) > ANALYST_1M_CLOSED_WINDOW:
            candles = candles[-ANALYST_1M_CLOSED_WINDOW:]
            frame["full_server_closed_count"] = original_count
            frame["closed_count"] = len(candles)
            frame["analysis_window_note"] = (
                "last 45 closed 1m bars for immediate sequencing; all longer "
                "history remains in complete 5m,15m,1h,4h candles and on the Paper server"
            )
        frame["candles"] = [[row.get(k) for k in OHLCV_COLUMNS] for row in candles]
        frame["candles_columns"] = list(OHLCV_COLUMNS)
        frame["candles_format"] = "arrays_utc_epoch_ms"
    return packed


def _nested_sections(before, after):
    def each_dict(value):
        return value.items() if isinstance(value, dict) else ()
    output = {}

    for tf in sorted(set(before["market"].get("timeframes", {}))
                     | set(after["market"].get("timeframes", {}))):
        a = before["market"].get("timeframes", {}).get(tf, {})
        b = after["market"].get("timeframes", {}).get(tf, {})
        output[f"candles:{tf}"] = {
            "before_chars": _size(a.get("candles", [])),
            "after_chars": _size(b.get("candles", [])),
            "before_count": len(a.get("candles") or []),
            "after_count": len(b.get("candles") or []),
        }
    catalog = before["evidence_catalog"]
    if isinstance(catalog, list):
        grouped = {}
        for item in catalog:
            if isinstance(item, dict):
                key = f"{item.get('kind','unknown')}:{item.get('timeframe','unknown')}"
                grouped.setdefault(key, []).append(item)
        output["evidence_catalog_by_kind_tf"] = {
            kind: {"count": len(items), "json_chars": _size(items)}
            for kind, items in sorted(grouped.items())
        }
    features = before["strategy_features"]
    if isinstance(features, dict):
        output["strategy_features_by_tf"] = {
            tf: {"json_chars": _size(value)}
            for tf, value in each_dict(features.get("timeframes"))
        }
    intelligence = before["market_intelligence"]
    if isinstance(intelligence, dict):
        output["market_intelligence_by_source"] = {
            src: {"json_chars": _size(value)}
            for src, value in each_dict(intelligence.get("sources"))
        }
        output["market_intelligence_other_chars"] = max(
            0, _size(intelligence) - sum(
                value["json_chars"] for value in
                output["market_intelligence_by_source"].values()
            )
        )
    return output


def build_analyst_payload(*, market, evidence_catalog, market_intelligence,
                          strategy_features, enabled_strategies, execution_regime,
                          instructions=""):
    before = {
        "market": market, "evidence_catalog": evidence_catalog,
        "market_intelligence": market_intelligence, "strategy_features": strategy_features,
        "enabled_strategies": enabled_strategies, "execution_regime": execution_regime,
    }
    after = {**before, "market": pack_market(market)}
    section_sizes = {
        key: {"before_chars": _size(value), "after_chars": _size(after[key]),
              "before_utf8_bytes": _bytes(value), "after_utf8_bytes": _bytes(after[key])}
        for key, value in before.items()
    }
    base = _size(before)
    compact = _size(after)
    input_text = _json(after)
    audit = {
        "version": "analyst_payload_measured_v2",
        "before_chars": base, "after_chars": compact,
        "saved_chars": base - compact,
        "reduction_pct": round(100 * (base - compact) / base, 2) if base else 0,
        "sections": section_sizes,
        "breakdown": _nested_sections(before, after),
        "candle_counts": {tf: len(frame.get("candles") or [])
                          for tf, frame in market.get("timeframes", {}).items()},
        "sent_candle_counts": {tf: len(frame.get("candles") or [])
                               for tf, frame in after["market"].get("timeframes", {}).items()},
        "instructions_chars": len(instructions),
        "instructions_utf8_bytes": len(instructions.encode("utf-8")),
        "request_text_chars": len(input_text) + len(instructions),
        "request_text_utf8_bytes": len(input_text.encode("utf-8")) + len(instructions.encode("utf-8")),
        "accounting_note": ("Character/byte totals and per-section sizes are exact for the constructed "
                            "request text, not exact token attribution. Provider usage reports aggregate tokens, "
                            "including cache-write tokens which are NOT an extra added input."),
        "policy": ("Keep every source candle on server and full 5m/15m/1h/4h analysis bars. "
                   "Retain last 45m 1m for near-term sequencing and pack numeric OHLCV arrays."),
    }
    return input_text, audit
