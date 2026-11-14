"""Static report PNG from immutable stored OHLC; never fetches market data or AI."""
from __future__ import annotations

import io
import json
import math
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from threading import Lock
from app.services.daily_outlook_geometry import nearby_levels, pivot_channel

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
    independent = context.get("analysis_mode") == "independent_htf_v1"
    four_hour = bool(candles.get("4h")) and not independent
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
    target_zones = []
    retests = []
    conditions = []
    referenced = set()
    for key in ("dominant", "alternative"):
        s = report.get(key) or {}
        activation = [levels[i]["price"] for i in s.get("activation_level_ids", []) if i in levels]
        text = s.get("activation_fa", "")
        # Only summarize an explicit, simple daily-close condition; never infer
        # a timeframe or confirmation rule from the scenario direction.
        condition = "Conditional: see report activation rules"
        if len(activation) == 1 and "بسته" in text and "روزانه" in text and not any(
            word in text for word in ("بازآزمایی", "سپس", "یا", "۴ساعته", "هفتگی")):
            side = "below" if "زیر" in text else "above" if "بالای" in text else None
            if side:
                condition = f"Conditional: daily close {side} {activation[0]:,.2f}"
        if any(w.get("field") == f"{key}.activation_fa"
               for w in report.get("text_validation_warnings", [])):
            condition = "Activation text conflict: review report"
        conditions.append({"kind": key, "text": condition, "direction": s.get("direction", "range")})
        for level_id in s.get("retest_level_ids") or []:
            if level_id in levels:
                retests.append({"kind": key, "price": levels[level_id]["price"]})
        if not s.get("target_level_ids"):
            continue
        for field in ("activation_level_ids", "target_level_ids", "invalidation_level_ids"):
            referenced.update(s.get(field) or [])
        ids = s.get("path_level_ids") or ((s.get("activation_level_ids") or []) + (s.get("target_level_ids") or []))
        if not ids or any(i not in levels for i in ids):
            continue
        values = []
        for i in ids:
            if not values or values[-1] != levels[i]["price"]:
                values.append(levels[i]["price"])
        zone_ids = s.get("target_zone_level_ids") or []
        if len(zone_ids) == 2 and all(i in levels for i in zone_ids):
            low, high = [levels[i]["price"] for i in zone_ids]
            if low < high and low <= values[-1] <= high:
                target_zones.append({"kind": key, "low": low, "high": high})
                referenced.update(zone_ids)
        points = [(0.0, quote)] + [(7 * (i + 1) / len(values), v) for i, v in enumerate(values)]
        paths.append({"kind": key, "direction": s.get("direction", "range"), "points": points})
        referenced.update(ids)
    nearby = nearby_levels(list(levels.values()), quote)
    referenced.update(l["id"] for side in nearby.values() for l in side)
    return {"nearby": nearby, "channel": ({"available": False, "reason": "independent_daily_chart"} if independent else context["chart_channel"] if "chart_channel" in context else pivot_channel(rows, anchor, step, quote)), "anchor": anchor, "quote": quote, "bars": rows, "step": step,
            "conditions": conditions, "paths": paths, "target_zones": target_zones, "retests": retests, "independent": independent, "levels": [l for key, l in levels.items() if key in referenced]}


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
        fig = Figure(figsize=(18, 9.5), dpi=120, facecolor="#0b1220")
        FigureCanvasAgg(fig)
        ax = fig.add_axes([.055, .20, .66, .62], facecolor="#101b2d")
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
        channel = data["channel"]
        if channel["available"]:
            slope = channel["slope_per_day"]
            for name, offset in (("upper",channel["upper"]), ("mid",channel["mid"]), ("lower",channel["lower"])):
                hx = [channel["start_day"], channel["end_day"]]
                hy = [offset+slope*x for x in hx]
                fy = [offset+slope*channel["end_day"], offset+slope*7]
                all_prices.extend(hy+fy)
                ax.plot(hx, hy, color="#80aaff", linewidth=1.1, alpha=.8, linestyle=":" if name=="mid" else "-",
                        label="Major-swing channel" if name=="upper" else None)
                ax.plot([channel["end_day"],7],fy,color="#80aaff",linewidth=.8,linestyle="-.",alpha=.5)
            anchor_side = 2 if channel.get("anchor_side") == "upper" else 3
            anchors = set(channel.get("anchor_times_ms", []))
            for row in data["bars"]:
                if row[0] in anchors:
                    ax.scatter([(row[0]/1000-anchor.timestamp())/DAY], [row[anchor_side]],
                               s=35, facecolors="none", edgecolors="#a8c4ff", linewidths=1.2, zorder=7)
            ax.fill_between([channel["start_day"],channel["end_day"]],
                [channel["lower"]+slope*channel["start_day"],channel["lower"]+slope*channel["end_day"]],
                [channel["upper"]+slope*channel["start_day"],channel["upper"]+slope*channel["end_day"]],color="#80aaff",alpha=.04)
        codes = {l["id"]:l["code"] for side in data["nearby"].values() for l in side}
        colors = {"dominant": "#5ce1bb", "alternative": "#e9b56d"}
        for retest in data["retests"]:
            all_prices.append(retest["price"])
            ax.hlines(retest["price"], 0, 7.3, color="#a9b8d1", linestyle=":", linewidth=1)
        if not any(p["kind"] == "dominant" for p in data["paths"]):
            ax.text(.98, .98, "Main scenario: no validated target", transform=ax.transAxes,
                    ha="right", va="top", color="#e9b56d", fontsize=10)
        for zone in data["target_zones"]:
            color = colors[zone["kind"]]
            all_prices.extend([zone["low"], zone["high"]])
            name = "Main" if zone["kind"] == "dominant" else "Alternative"
            ax.add_patch(Rectangle((0, zone["low"]), 7.3, zone["high"]-zone["low"],
                                   facecolor=color, edgecolor=color, alpha=.16, zorder=2,
                                   label=f"{name} target zone: {zone['low']:,.2f} - {zone['high']:,.2f}"))
        for path in data["paths"]:
            color = colors[path["kind"]]
            xs, ys = zip(*path["points"])
            all_prices.extend(ys)
            name = "Main scenario" if path["kind"] == "dominant" else "Alternative scenario"
            ax.plot(xs, ys, color=color, linestyle="--", linewidth=2.2, marker="o", markersize=3,
                    label=f"{name} ({path['direction']})", zorder=5)
            # The endpoint marker is authoritative; text lives in the side panel.
            ax.annotate("", xy=path["points"][-1], xytext=path["points"][-2],
                        arrowprops={"arrowstyle": "-|>", "color": color, "lw": 1.3, "linestyle": "--"})
        seen = set()
        for level in data["levels"]:
            price = level["price"]
            if price in seen:
                continue
            seen.add(price)
            all_prices.append(price)
            code = codes.get(level["id"], "")
            color = "#62c9bb" if code.startswith("S") else "#f18d9f" if code.startswith("R") else "#7890ad"
            ax.axhline(price, color=color, linestyle=":", linewidth=.8, alpha=.65)
        # Fixed side-panel rows keep labels away from candles and path arrows.
        for index, condition in enumerate(data["conditions"]):
            kind = condition["kind"]
            color = colors[kind]
            y = .79 - index * .25
            name = "MAIN" if kind == "dominant" else "ALTERNATIVE"
            fig.text(.77, y, f"{name} / {condition['direction'].upper()}",
                     color=color, fontsize=12, weight="bold")
            fig.text(.77, y-.035, condition["text"], color="#c8d7ee", fontsize=9)
            path = next((p for p in data["paths"] if p["kind"] == kind), None)
            target = f"{path['points'][-1][1]:,.2f}" if path else "No validated target"
            fig.text(.77, y-.07, f"Final target: {target}", color=color, fontsize=11)
            zone = next((z for z in data["target_zones"] if z["kind"] == kind), None)
            if zone:
                fig.text(.77, y-.105, f"Zone: {zone['low']:,.2f} - {zone['high']:,.2f}",
                         color=color, fontsize=10)
            if path and len(path["points"]) > 2:
                via = " / ".join(f"{v:,.2f}" for _, v in path["points"][1:-1])
                import textwrap
                fig.text(.77, y-.14, textwrap.fill("Via: " + via, 34), color="#c8d7ee", fontsize=9)
        retest_prices = sorted({r["price"] for r in data["retests"]})
        if retest_prices:
            import textwrap
            fig.text(.77, .25, textwrap.fill("Retest levels: " + " / ".join(
                f"{p:,.2f}" for p in retest_prices), 34), color="#a9b8d1", fontsize=9)
        if payload["report"].get("text_validation_warnings"):
            fig.text(.055, .135, "TEXT WARNING: a condition price conflicts with its referenced level. Review report before use.",
                     color="#ffbd75", fontsize=10)
        ax.scatter([0], [data["quote"]], color="white", s=20, zorder=8)
        pad = max((max(all_prices)-min(all_prices))*.12, data["quote"]*.003)
        ax.set_ylim(min(all_prices)-pad, max(all_prices)+pad)
        ax.set_xlim(start-.4, 8)
        ticks = [start+(0-start)*i/4 for i in range(4)] + [0, 2, 4, 7]
        ax.set_xticks(ticks)
        ax.set_xticklabels([(("PATH" if d == 7 else "") if d > 0 and data["independent"] else (anchor+timedelta(days=d)).strftime("%d %b") if d else "REPORT\n"+anchor.strftime("%d %b")) for i, d in enumerate(ticks)])
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.yaxis.tick_right()
        ax.tick_params(colors="#abbdd7", labelsize=9)
        ax.grid(axis="y", color="white", alpha=.06)
        for spine in ax.spines.values():
            spine.set_visible(False)
        if data["paths"] or channel["available"]:
            legend = ax.legend(loc="upper left", facecolor="#101b2d", edgecolor="#2a3c56", fontsize=9)
            for text in legend.get_texts():
                text.set_color("#d7e3f4")
        symbol = str(payload.get("symbol") or "BTCUSDT")
        fig.text(.055, .94, f"{symbol}  /  {'4H' if payload['context'].get('report_cadence') == '4h' else 'DAILY'} MARKET OUTLOOK", color="white", fontsize=20, weight="bold")
        fig.text(.055, .892, f"Report #{payload['id']}  |  {payload['date']}  |  Bitunix {'4H' if data['step']==14400 else '1D'} closed candles", color="#b4c5df", fontsize=11)
        fig.text(.055, .855, f"Snapshot: {anchor.strftime('%Y-%m-%d %H:%M UTC')}  |  Price: {data['quote']:,.2f}  |  Schematic path (not arrival dates)", color="#b4c5df", fontsize=10)
        fig.text(.055, .10, "Green: main scenario | Amber: alternative | Shading: grounded target zone (when available)" if data["independent"] else "S: candidate support | R: candidate resistance | Blue: pivot channel; future extension is conditional", color="#c8d7ee", fontsize=10)
        fig.text(.055, .065, "Waypoint spacing is schematic, not predicted arrival times. No future candles are generated.", color="#e9b56d", fontsize=10)
        warning = bool(payload["context"].get("data_quality_warnings"))
        fig.text(.055, .03, "Historical source anomaly disclosed in report." if warning else "Frozen report snapshot. Opening this chart does not refresh market data or call AI.", color="#91a5c2", fontsize=9)
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=120, facecolor=fig.get_facecolor(), metadata={"Software":"TradeOnBU daily report renderer"})
        fig.clear()
        return buffer.getvalue()
