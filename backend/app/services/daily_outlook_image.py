"""Static report PNG from immutable stored OHLC; never fetches market data or AI."""
from __future__ import annotations

import io
import json
import math
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from threading import Lock

_RENDER_LOCK = Lock()
DAY = 86400



def chart_snapshot(context: dict, report: dict) -> dict:
    anchor = datetime.fromisoformat(context["as_of"].replace("Z", "+00:00"))
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor = anchor.astimezone(timezone.utc)
    quote = float(context["live_price"])
    if not math.isfinite(quote) or quote <= 0:
        raise ValueError("invalid_snapshot_quote")
    candles = context.get("candles") or {}
    four_hour = bool(candles.get("4h"))
    step = 14400 if four_hour else DAY
    raw = (candles.get("4h") if four_hour else candles.get("1d")) or []
    bars = {}
    for row in raw[-120 if four_hour else -30:]:
        if len(row) < 5 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in row[:5]):
            raise ValueError("invalid_snapshot_candle")
        ms, op, hi, lo, cl = row[:5]
        if min(op, hi, lo, cl) <= 0 or lo > min(op, cl) or hi < max(op, cl):
            raise ValueError("invalid_snapshot_candle")
        if ms / 1000 + step > anchor.timestamp():
            continue  # A forming candle must never be rendered as historical evidence.
        bars[ms] = [ms, op, hi, lo, cl]
    rows = [bars[t] for t in sorted(bars)]
    if not rows:
        raise ValueError("missing_snapshot_candles")
    levels = {l["id"]: l for l in context.get("levels", [])
              if isinstance(l.get("price"), (int, float)) and math.isfinite(l["price"]) and l["price"] > 0}
    paths = []
    referenced = set()
    for key in ("dominant", "alternative"):
        s = report.get(key) or {}
        for field in ("activation_level_ids", "target_level_ids", "invalidation_level_ids"):
            referenced.update(s.get(field) or [])
        ids = s.get("path_level_ids") or ((s.get("activation_level_ids") or []) + (s.get("target_level_ids") or []))
        if not ids or any(i not in levels for i in ids):
            continue
        values = []
        for i in ids:
            if not values or values[-1] != levels[i]["price"]:
                values.append(levels[i]["price"])
        points = [(0.0, quote)] + [(7 * (i + 1) / len(values), v) for i, v in enumerate(values)]
        paths.append({"kind": key, "direction": s.get("direction", "range"), "points": points})
        referenced.update(ids)
    return {"anchor": anchor, "quote": quote, "bars": rows, "step": step,
            "paths": paths, "levels": [l for key, l in levels.items() if key in referenced]}


@lru_cache(maxsize=8)
def render_report_image(snapshot_json: str) -> bytes:
    payload = json.loads(snapshot_json)
    data = chart_snapshot(payload["context"], payload["report"])
    # Matplotlib rendering is serialized; HTTP handlers dispatch this off the event loop.
    with _RENDER_LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.patches import Rectangle
        from matplotlib.ticker import FuncFormatter
        fig = Figure(figsize=(15, 8.5), dpi=120, facecolor="#0b1220")
        FigureCanvasAgg(fig)
        ax = fig.add_axes([.055, .17, .87, .65], facecolor="#101b2d")
        anchor = data["anchor"]
        start = (data["bars"][0][0] / 1000 - anchor.timestamp()) / DAY
        width = data["step"] / DAY * .65
        for ms, op, hi, lo, cl in data["bars"]:
            x = (ms / 1000 - anchor.timestamp()) / DAY
            color = "#55d6ae" if cl >= op else "#f38396"
            ax.vlines(x, lo, hi, color=color, linewidth=.9)
            ax.add_patch(Rectangle((x-width/2, min(op, cl)), width, abs(cl-op), facecolor=color, edgecolor=color, linewidth=.8))
            if cl == op:
                ax.hlines(cl, x-width/2, x+width/2, color=color, linewidth=1)
        ax.axvspan(0, 7.3, color="#91a5c9", alpha=.055)
        ax.axvline(0, color="#a9b8d1", linestyle=":", linewidth=1.2)
        all_prices = [v for b in data["bars"] for v in (b[2], b[3])] + [data["quote"]]
        colors = {"dominant": "#5ce1bb", "alternative": "#e9b56d"}
        for path in data["paths"]:
            color = colors[path["kind"]]
            xs, ys = zip(*path["points"])
            all_prices.extend(ys)
            name = "A: Dominant" if path["kind"] == "dominant" else "B: Alternative"
            ax.plot(xs, ys, color=color, linestyle="--", linewidth=2.2, marker="o", markersize=3,
                    label=f"{name} ({path['direction']})", zorder=5)
            ax.annotate("", xy=path["points"][-1], xytext=path["points"][-2],
                        arrowprops={"arrowstyle": "-|>", "color": color, "lw": 1.3, "linestyle": "--"})
        seen = set()
        for level in data["levels"]:
            price = level["price"]
            if price in seen:
                continue
            seen.add(price)
            all_prices.append(price)
            ax.axhline(price, color="#7890ad", linestyle=":", linewidth=.65, alpha=.65)
            # Prices printed on the right avoid a stack of long labels across candles.
            ax.annotate(f"{price:,.2f}", xy=(7.15, price), fontsize=7, color="#adc0d9", va="bottom", ha="right",
                        bbox={"facecolor":"#101b2d", "edgecolor":"none", "alpha":.8, "pad":1})
        ax.scatter([0], [data["quote"]], color="white", s=20, zorder=8)
        pad = max((max(all_prices)-min(all_prices))*.12, data["quote"]*.003)
        ax.set_ylim(min(all_prices)-pad, max(all_prices)+pad)
        ax.set_xlim(start-.4, 8)
        ticks = [start+(0-start)*i/4 for i in range(4)] + [0, 2, 4, 7]
        ax.set_xticks(ticks)
        ax.set_xticklabels([(anchor+timedelta(days=d)).strftime("%d %b") if d else "REPORT\n"+anchor.strftime("%d %b") for d in ticks])
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.yaxis.tick_right()
        ax.tick_params(colors="#abbdd7", labelsize=9)
        ax.grid(axis="y", color="white", alpha=.06)
        for spine in ax.spines.values():
            spine.set_visible(False)
        if data["paths"]:
            legend = ax.legend(loc="upper left", facecolor="#101b2d", edgecolor="#2a3c56", fontsize=9)
            for text in legend.get_texts():
                text.set_color("#d7e3f4")
        symbol = str(payload.get("symbol") or "BTCUSDT")
        fig.text(.055, .94, f"{symbol}  /  DAILY MARKET OUTLOOK", color="white", fontsize=20, weight="bold")
        fig.text(.055, .892, f"Report #{payload['id']}  |  {payload['date']}  |  Bitunix {'4H' if data['step']==14400 else '1D'} closed candles", color="#b4c5df", fontsize=11)
        fig.text(.055, .855, f"Snapshot: {anchor.strftime('%Y-%m-%d %H:%M UTC')}  |  Price: {data['quote']:,.2f}  |  Horizon: 7 days", color="#b4c5df", fontsize=10)
        fig.text(.055, .10, "REAL STORED CANDLES  |  Dashed lines: conditional scenarios  |  Horizontal lines: report levels", color="#c8d7ee", fontsize=10)
        fig.text(.055, .065, "Waypoint spacing is schematic, not predicted arrival times. No future candles are generated.", color="#e9b56d", fontsize=10)
        warning = bool(payload["context"].get("data_quality_warnings"))
        fig.text(.055, .03, "Historical source anomaly disclosed in report." if warning else "Frozen report snapshot. Opening this chart does not refresh market data or call AI.", color="#91a5c2", fontsize=9)
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=120, facecolor=fig.get_facecolor(), metadata={"Software":"TradeOnBU daily report renderer"})
        fig.clear()
        return buffer.getvalue()
