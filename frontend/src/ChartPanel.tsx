import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeriesPartialOptions,
  createChart,
  IChartApi,
  ISeriesApi,
  IPriceLine,
  LineStyle,
} from "lightweight-charts";
import { apiFetch } from "./api";

type Candle = { time_ms: number; open: number; high: number; low: number; close: number };
type Overlays = {
  swings: { time_ms: number; price: number; kind: string }[];
  bos: string | null;
  mss: string | null;
  liquidity_levels: number[];
  liquidity_sweep: { time_ms: number; level: number } | null;
  fvg_zones: { low: number; high: number; direction: string; time_ms: number; mitigated: boolean }[];
};

export type SignalLevels = {
  entry: number;
  sl: number;
  tp: number;
} | null;

const intervals = ["1m", "5m", "15m", "1h", "4h"] as const;

function levelsKey(levels: SignalLevels): string {
  if (!levels) return "";
  return `${levels.entry}:${levels.sl}:${levels.tp}`;
}

export function ChartPanel({
  symbol,
  direction,
  signalLevels,
}: {
  symbol: string;
  direction?: string;
  signalLevels: SignalLevels;
}) {
  const [interval, setInterval] = useState<(typeof intervals)[number]>("5m");
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const levelsKeyMemo = useMemo(() => levelsKey(signalLevels), [signalLevels]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
      grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
      height: 460,
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    const opts: CandlestickSeriesPartialOptions = {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    };
    const series = chart.addCandlestickSeries(opts);
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let abort: AbortController | null = null;

    async function load(isPoll = false) {
      if (!isPoll) setLoading(true);
      abort?.abort();
      abort = new AbortController();
      setError("");
      try {
        const q = new URLSearchParams({ symbol, interval });
        if (direction) q.set("direction", direction);
        const data = await apiFetch(`/api/market/chart?${q.toString()}`, { signal: abort.signal }, 30_000);
        if (cancelled || !seriesRef.current || !chartRef.current) return;

        const candles = (data.candles as Candle[]) || [];
        if (!candles.length) {
          setError("داده کندلی در دسترس نیست — بعداً دوباره تلاش کنید");
          seriesRef.current.setData([]);
          return;
        }

        seriesRef.current.setData(
          candles.map((c) => ({
            time: Math.floor(c.time_ms / 1000) as any,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          }))
        );

        for (const line of priceLinesRef.current) {
          seriesRef.current.removePriceLine(line);
        }
        priceLinesRef.current = [];

        const overlays = data.overlays as Overlays;
        for (const fvg of overlays?.fvg_zones || []) {
          priceLinesRef.current.push(
            seriesRef.current.createPriceLine({
              price: fvg.low,
              color: fvg.mitigated ? "#555" : "#6dffb5",
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              title: `FVG ${fvg.direction}`,
            })
          );
          priceLinesRef.current.push(
            seriesRef.current.createPriceLine({
              price: fvg.high,
              color: fvg.mitigated ? "#555" : "#6dffb5",
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              title: "FVG",
            })
          );
        }
        for (const sw of overlays?.swings || []) {
          priceLinesRef.current.push(
            seriesRef.current.createPriceLine({
              price: sw.price,
              color: sw.kind === "HIGH" ? "#ff9f43" : "#54a0ff",
              lineWidth: 1,
              lineStyle: LineStyle.SparseDotted,
              title: sw.kind,
            })
          );
        }
        if (signalLevels) {
          for (const [price, title, color] of [
            [signalLevels.entry, "Entry", "#ffd76a"],
            [signalLevels.sl, "SL", "#ff6b6b"],
            [signalLevels.tp, "TP", "#6dffb5"],
          ] as const) {
            priceLinesRef.current.push(
              seriesRef.current.createPriceLine({
                price,
                color,
                lineWidth: 2,
                lineStyle: LineStyle.Dashed,
                title,
              })
            );
          }
        }
        chartRef.current.timeScale().fitContent();
      } catch (e) {
        if (cancelled || (e instanceof DOMException && e.name === "AbortError")) return;
        setError(e instanceof Error ? e.message : "Chart load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load(false);
    const id = window.setInterval(() => load(true), 45_000);
    return () => {
      cancelled = true;
      abort?.abort();
      window.clearInterval(id);
    };
  }, [symbol, interval, direction, levelsKeyMemo]);

  return (
    <div className="card">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <h2 style={{ flex: 1 }}>چارت کندلی واقعی Bitunix</h2>
        {intervals.map((iv) => (
          <button
            key={iv}
            type="button"
            className={iv === interval ? "" : "secondary"}
            style={{ width: "auto", padding: "0.35rem 0.7rem" }}
            onClick={() => setInterval(iv)}
          >
            {iv}
          </button>
        ))}
      </div>
      {loading && !error && <p className="hint">در حال بارگذاری چارت…</p>}
      {error && <p className="stale">{error}</p>}
      <div className="chart-wrap" ref={containerRef} />
      <p style={{ color: "#9fb0d0", fontSize: "0.82rem" }}>
        BOS: {direction || "—"} | Overlays: Swing, FVG, Sweep, Entry/SL/TP — داده واقعی API
      </p>
    </div>
  );
}
