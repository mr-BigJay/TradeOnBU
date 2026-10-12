import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeriesPartialOptions,
  createChart,
  IChartApi,
  ISeriesApi,
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

type SignalLevels = {
  entry: number;
  sl: number;
  tp: number;
} | null;

const intervals = ["1m", "5m", "15m", "1h", "4h"] as const;

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
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError("");
      try {
        const q = new URLSearchParams({ symbol, interval });
        if (direction) q.set("direction", direction);
        const data = await apiFetch(`/api/market/chart?${q.toString()}`);
        if (cancelled || !containerRef.current) return;

        chartRef.current?.remove();
        const chart = createChart(containerRef.current, {
          layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
          grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
          height: 460,
          timeScale: { timeVisible: true, secondsVisible: false },
        });
        chartRef.current = chart;
        const opts: CandlestickSeriesPartialOptions = {
          upColor: "#26a69a",
          downColor: "#ef5350",
          borderVisible: false,
          wickUpColor: "#26a69a",
          wickDownColor: "#ef5350",
        };
        const series = chart.addCandlestickSeries(opts);
        seriesRef.current = series;
        series.setData(
          (data.candles as Candle[]).map((c) => ({
            time: Math.floor(c.time_ms / 1000) as any,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          }))
        );

        const overlays = data.overlays as Overlays;
        for (const fvg of overlays.fvg_zones || []) {
          series.createPriceLine({
            price: fvg.low,
            color: fvg.mitigated ? "#555" : "#6dffb5",
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            title: `FVG ${fvg.direction}`,
          });
          series.createPriceLine({
            price: fvg.high,
            color: fvg.mitigated ? "#555" : "#6dffb5",
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            title: "FVG",
          });
        }
        for (const sw of overlays.swings || []) {
          series.createPriceLine({
            price: sw.price,
            color: sw.kind === "HIGH" ? "#ff9f43" : "#54a0ff",
            lineWidth: 1,
            lineStyle: LineStyle.SparseDotted,
            title: sw.kind,
          });
        }
        if (signalLevels) {
          for (const [price, title, color] of [
            [signalLevels.entry, "Entry", "#ffd76a"],
            [signalLevels.sl, "SL", "#ff6b6b"],
            [signalLevels.tp, "TP", "#6dffb5"],
          ] as const) {
            series.createPriceLine({
              price,
              color,
              lineWidth: 2,
              lineStyle: LineStyle.Dashed,
              title,
            });
          }
        }
        chart.timeScale().fitContent();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Chart load failed");
      }
    }
    load();
    const id = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
      chartRef.current?.remove();
    };
  }, [symbol, interval, direction, signalLevels]);

  return (
    <div className="card">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <h2 style={{ flex: 1 }}>چارت کندلی واقعی Bitunix</h2>
        {intervals.map((iv) => (
          <button
            key={iv}
            className={iv === interval ? "" : "secondary"}
            style={{ width: "auto", padding: "0.35rem 0.7rem" }}
            onClick={() => setInterval(iv)}
          >
            {iv}
          </button>
        ))}
      </div>
      {error && <p className="stale">{error}</p>}
      <div className="chart-wrap" ref={containerRef} />
      <p style={{ color: "#9fb0d0", fontSize: "0.82rem" }}>
        BOS: {direction || "—"} | Overlays: Swing, FVG, Sweep, Entry/SL/TP — داده واقعی API
      </p>
    </div>
  );
}
