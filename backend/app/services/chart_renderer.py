from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from app.domain.candles import Candle

logger = logging.getLogger(__name__)

CHART_DPI = 120
CHART_WIDTH = 10
CHART_HEIGHT = 6


class ChartRenderError(Exception):
    pass


def _sanitize_candles(candles: list[Candle]) -> list[Candle]:
    if not candles:
        raise ChartRenderError("no_candles")
    by_time: dict[int, Candle] = {}
    for c in candles:
        if c.time_ms <= 0:
            continue
        if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
            continue
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            continue
        by_time[c.time_ms] = c
    ordered = [by_time[t] for t in sorted(by_time.keys())]
    if len(ordered) < 10:
        raise ChartRenderError("insufficient_valid_candles")
    return ordered[-120:]


def _candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    rows = []
    for c in candles:
        ts = datetime.fromtimestamp(c.time_ms / 1000.0, tz=timezone.utc)
        rows.append(
            {
                "Date": ts,
                "Open": c.open,
                "High": c.high,
                "Low": c.low,
                "Close": c.close,
                "Volume": 0.0,
            }
        )
    df = pd.DataFrame(rows).set_index("Date")
    return df


def _time_to_index(df: pd.DataFrame, time_ms: int | None) -> datetime | None:
    if time_ms is None:
        return None
    ts = datetime.fromtimestamp(time_ms / 1000.0, tz=timezone.utc)
    if ts in df.index:
        return ts
    # nearest index
    idx = df.index.get_indexer([ts], method="nearest")
    if idx.size and idx[0] >= 0:
        return df.index[int(idx[0])]
    return None


def _format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:.4f}"


def render_ict_chart_png(
    *,
    output_path: Path,
    symbol: str,
    interval: str,
    candles: list[Candle],
    overlays: dict[str, Any] | None,
    live_price: float,
    decision: str,
    trade_levels: dict[str, float] | None,
    analyzed_at: datetime,
) -> None:
    """Render candlestick PNG with ICT overlays and optional trade levels."""
    clean = _sanitize_candles(candles)
    df = _candles_to_df(clean)
    overlays = overlays or {}
    decision_u = (decision or "NO_TRADE").upper()

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        gridcolor="#2a3548",
        facecolor="#0b1018",
        edgecolor="#2a3548",
        figcolor="#0b1018",
        rc={
            "axes.labelcolor": "#cbd5e1",
            "xtick.color": "#94a3b8",
            "ytick.color": "#94a3b8",
            "font.size": 9,
        },
    )

    addplots = []
    if live_price > 0:
        addplots.append(
            mpf.make_addplot(
                [live_price] * len(df),
                color="#38bdf8",
                linestyle="--",
                width=1.0,
                alpha=0.85,
            )
        )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        volume=False,
        figsize=(CHART_WIDTH, CHART_HEIGHT),
        returnfig=True,
        addplot=addplots if addplots else None,
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        tight_layout=True,
    )
    ax = axes[0] if isinstance(axes, list) else axes

    # Trade levels (only when provided — caller must not invent)
    level_colors = {
        "entry": "#fbbf24",
        "stop_loss": "#f87171",
        "take_profit": "#4ade80",
    }
    last_x = len(df) - 1

    if trade_levels:
        for key, color in level_colors.items():
            val = trade_levels.get(key)
            if val is None or val <= 0:
                continue
            ax.axhline(val, color=color, linewidth=1.2, linestyle="-", alpha=0.9)
            ax.text(
                last_x,
                val,
                f" {key.replace('_', ' ').upper()} {_format_price(val)}",
                color=color,
                fontsize=8,
                va="center",
                ha="left",
            )

    # Liquidity levels from structure analysis
    for lvl in overlays.get("liquidity_levels") or []:
        try:
            price = float(lvl)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        ax.axhline(price, color="#64748b", linewidth=0.8, linestyle=":", alpha=0.65)

    def _xaxis_fraction(ts: datetime) -> float:
        loc = df.index.get_loc(ts)
        if isinstance(loc, slice):
            idx = loc.start if loc.start is not None else 0
        elif isinstance(loc, (list, tuple)):
            idx = int(loc[0]) if loc else 0
        else:
            idx = int(loc)
        return max(0.0, min(1.0, idx / max(len(df) - 1, 1)))

    # FVG zones
    for zone in overlays.get("fvg_zones") or []:
        try:
            z_low = float(zone["low"])
            z_high = float(zone["high"])
        except (KeyError, TypeError, ValueError):
            continue
        if z_low <= 0 or z_high <= 0:
            continue
        t_start = _time_to_index(df, zone.get("time_ms"))
        if t_start is None:
            t_start = df.index[max(0, len(df) - 20)]
        color = "#a78bfa" if str(zone.get("direction", "")).upper() == "BULLISH" else "#fb7185"
        ax.axhspan(
            z_low,
            z_high,
            xmin=_xaxis_fraction(t_start),
            xmax=1.0,
            color=color,
            alpha=0.18,
        )

    # Liquidity sweep marker
    sweep = overlays.get("liquidity_sweep")
    if isinstance(sweep, dict) and sweep.get("time_ms"):
        t_sw = _time_to_index(df, int(sweep["time_ms"]))
        if t_sw is not None:
            loc = df.index.get_loc(t_sw)
            if isinstance(loc, slice):
                x_pos = loc.start if loc.start is not None else 0
            elif isinstance(loc, (list, tuple)):
                x_pos = int(loc[0]) if loc else 0
            else:
                x_pos = int(loc)
            ax.axvline(x_pos, color="#e879f9", linewidth=1.0, linestyle="--", alpha=0.75)

    # Title / branding
    ts_label = analyzed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bos = overlays.get("bos")
    mss = overlays.get("mss")
    structure_bits = []
    if bos:
        structure_bits.append(f"BOS {bos}")
    if mss:
        structure_bits.append(f"MSS {mss}")
    structure_line = " · ".join(structure_bits) if structure_bits else "Structure n/a"

    title = f"TradeOnBU · {symbol} · {interval} · {decision_u}"
    subtitle = f"{structure_line} · Live {_format_price(live_price)} · {ts_label} · PAPER ONLY"
    fig.suptitle(title, color="#f1f5f9", fontsize=12, fontweight="bold", y=0.98)
    ax.set_title(subtitle, color="#94a3b8", fontsize=9, pad=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=CHART_DPI, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info("Chart saved run chart %s", output_path.name)
