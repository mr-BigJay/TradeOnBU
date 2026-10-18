import { useCallback, useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, LineStyle, UTCTimestamp } from "lightweight-charts";
import { apiFetch } from "./api";

type Candle = { time_ms: number; open: number; high: number; low: number; close: number };
type ForecastPoint = { time: string; price: number; actual_close?: number | null; error_pct?: number | null };

type ChartPayload = {
  candles: Candle[];
  anchor_price: number | null;
  anchor_time: string | null;
  forecast_points: ForecastPoint[];
  generated_at: string | null;
  error?: string | null;
  collector_freshness?: Record<string, string> | null;
};

export function ForecastChart({ refreshKey = 0 }: { refreshKey?: number }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const anchorTimeRef = useRef<string | null>(null);
  const [error, setError] = useState("");
  const [meta, setMeta] = useState<ChartPayload | null>(null);
  const [loading, setLoading] = useState(true);

  const layoutFutureOverlay = useCallback(() => {
    const overlay = overlayRef.current;
    const chart = chartRef.current;
    const wrap = wrapRef.current;
    const anchorTime = anchorTimeRef.current;
    if (!overlay || !chart || !wrap || !anchorTime) {
      if (overlay) overlay.style.width = "0px";
      return;
    }
    const anchorSec = Math.floor(new Date(anchorTime).getTime() / 1000) as UTCTimestamp;
    const x = chart.timeScale().timeToCoordinate(anchorSec);
    if (x == null) return;
    overlay.style.left = `${x}px`;
    overlay.style.width = `${Math.max(0, wrap.clientWidth - x)}px`;
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
      grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
      height: 420,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 18 },
      crosshair: { mode: 1 },
    });
    const candles = chart.addCandlestickSeries({
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    });
    const forecastLine = chart.addLineSeries({
      color: "#00e5ff",
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      crosshairMarkerVisible: true,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chartRef.current = chart;
    candleRef.current = candles;
    lineRef.current = forecastLine;

    const ro = new ResizeObserver(() => {
      const node = containerRef.current;
      if (!node) return;
      chart.applyOptions({ width: node.clientWidth });
      layoutFutureOverlay();
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      lineRef.current = null;
    };
  }, [layoutFutureOverlay]);

  useEffect(() => {
    layoutFutureOverlay();
  }, [meta, layoutFutureOverlay]);

  useEffect(() => {
    let cancelled = false;
    async function load(isPoll = false) {
      if (!isPoll) setLoading(true);
      setError("");
      try {
        const data = (await apiFetch("/api/gui/forecast/chart", {}, 30_000)) as ChartPayload;
        if (cancelled) return;
        setMeta(data);
        anchorTimeRef.current = data.anchor_time;
        if (!candleRef.current || !lineRef.current || !chartRef.current) return;

        const candles = data.candles || [];
        candleRef.current.setData(
          candles.map((c) => ({
            time: Math.floor(c.time_ms / 1000) as UTCTimestamp,
            open: c.open,
            high: c.high,
            low: c.low,
            close: c.close,
          }))
        );

        if (data.anchor_price && data.anchor_time && data.forecast_points?.length === 6) {
          const anchorSec = Math.floor(new Date(data.anchor_time).getTime() / 1000) as UTCTimestamp;
          const path = [
            { time: anchorSec, value: data.anchor_price },
            ...data.forecast_points.map((p) => ({
              time: Math.floor(new Date(p.time).getTime() / 1000) as UTCTimestamp,
              value: p.price,
            })),
          ];
          lineRef.current.setData(path);
          lineRef.current.setMarkers(
            data.forecast_points.map((p) => ({
              time: Math.floor(new Date(p.time).getTime() / 1000) as UTCTimestamp,
              position: "inBar" as const,
              color: "#00e5ff",
              shape: "circle" as const,
              text: `${p.price.toFixed(1)}`,
            }))
          );
          const lastT = path[path.length - 1].time as number;
          const firstT = candles.length ? (Math.floor(candles[0].time_ms / 1000) as number) : lastT - 3600 * 48;
          chartRef.current.timeScale().setVisibleRange({
            from: firstT as UTCTimestamp,
            to: (lastT + 3600) as UTCTimestamp,
          });
          layoutFutureOverlay();
        } else {
          anchorTimeRef.current = null;
          lineRef.current.setData([]);
          lineRef.current.setMarkers([]);
          chartRef.current.timeScale().fitContent();
          layoutFutureOverlay();
        }

        if (data.error && data.error !== "no_successful_forecast" && data.error !== "no_active_forecast") {
          setError(data.error);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Chart load failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load(false);
    const id = window.setInterval(() => load(true), 45_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [refreshKey, layoutFutureOverlay]);

  return (
    <div className="card forecast-chart-card">
      <h2>مسیر پیش‌بینی ۶ ساعته (1H)</h2>
      {loading && <p className="hint">در حال بارگذاری چارت…</p>}
      {error && <p className="stale">{error}</p>}
      {meta?.generated_at && (
        <p className="hint">آخرین پیش‌بینی: {new Date(meta.generated_at).toLocaleString("fa-IR")}</p>
      )}
      <div className="forecast-chart-wrap" ref={wrapRef}>
        <div className="forecast-future-shade" ref={overlayRef} aria-hidden />
        <div className="chart-wrap" ref={containerRef} />
      </div>
      {meta?.collector_freshness && (
        <p className="hint" style={{ fontSize: "0.8rem" }}>
          وضعیت داده: {Object.entries(meta.collector_freshness).map(([k, v]) => `${k}:${v}`).join(" · ")}
        </p>
      )}
      <p style={{ color: "#9fb0d0", fontSize: "0.82rem" }}>
        خط فیروزه‌ای فقط پیش‌بینی AI است — کندل‌های آینده ساخته نمی‌شوند.
      </p>
    </div>
  );
}
