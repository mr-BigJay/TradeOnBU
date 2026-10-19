import { useCallback, useEffect, useRef, useState } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  LineStyle,
  MouseEventParams,
  Time,
  UTCTimestamp,
} from "lightweight-charts";
import { apiFetch } from "./api";
import {
  buildForecastPath,
  computeInitialVisibleRange,
  findForecastPointIndex,
  forecastHourLabel,
  formatUsd,
  pctChangeFromAnchor,
  toUtcSec,
} from "./forecastChartView";

type Candle = { time_ms: number; open: number; high: number; low: number; close: number };
type ForecastPoint = { time: string; price: number; actual_close?: number | null; error_pct?: number | null };

type ChartPayload = {
  candles: Candle[];
  anchor_price: number | null;
  anchor_time: string | null;
  forecast_points: ForecastPoint[];
  generated_at: string | null;
  run_id?: string | null;
  error?: string | null;
  collector_freshness?: Record<string, string> | null;
};

type ActiveForecast = {
  anchorPrice: number;
  anchorTimeIso: string;
  points: ForecastPoint[];
  runId: string | null;
};

const FORECAST_COLOR = "#00e5ff";

export function ForecastChart({ refreshKey = 0 }: { refreshKey?: number }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const boundaryRef = useRef<HTMLDivElement | null>(null);
  const zoneLabelRef = useRef<HTMLDivElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);

  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const anchorTimeRef = useRef<string | null>(null);
  const activeForecastRef = useRef<ActiveForecast | null>(null);
  const userRangeRef = useRef(false);
  const lastAppliedRunRef = useRef<string | null>(null);
  const suppressRangeRef = useRef(false);

  const [error, setError] = useState("");
  const [meta, setMeta] = useState<ChartPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; html: string } | null>(null);

  const layoutForecastChrome = useCallback(() => {
    const chart = chartRef.current;
    const wrap = wrapRef.current;
    const overlay = overlayRef.current;
    const boundary = boundaryRef.current;
    const label = zoneLabelRef.current;
    const anchorTime = anchorTimeRef.current;

    if (!chart || !wrap || !overlay || !boundary || !label) return;

    if (!anchorTime) {
      overlay.style.width = "0px";
      boundary.style.left = "0px";
      label.style.opacity = "0";
      return;
    }

    const anchorSec = toUtcSec(anchorTime) as UTCTimestamp;
    const x = chart.timeScale().timeToCoordinate(anchorSec);
    if (x == null) {
      overlay.style.width = "0px";
      return;
    }

    const left = Math.max(0, x);
    overlay.style.left = `${left}px`;
    overlay.style.width = `${Math.max(0, wrap.clientWidth - left)}px`;
    boundary.style.left = `${left}px`;
    label.style.left = `${left + 10}px`;
    label.style.opacity = "1";
  }, []);

  const handleCrosshairMove = useCallback((param: MouseEventParams<Time>) => {
    const wrap = wrapRef.current;
    const forecast = activeForecastRef.current;
    if (!wrap || !forecast || !param.point || param.time === undefined) {
      setTooltip(null);
      return;
    }

    const idx = findForecastPointIndex(param.time as number, forecast.points);
    if (idx === null) {
      setTooltip(null);
      return;
    }

    const pt = forecast.points[idx];
    const utc = new Date(pt.time);
    const local = new Date(pt.time);
    const title = `${forecastHourLabel(idx)} — AI Forecast`;
    const html = [
      `<div class="forecast-point-tooltip__title">${title}</div>`,
      `<div class="forecast-point-tooltip__row">Predicted: <strong>${formatUsd(pt.price)}</strong></div>`,
      `<div class="forecast-point-tooltip__row">Change from Anchor: <strong>${pctChangeFromAnchor(pt.price, forecast.anchorPrice)}</strong></div>`,
      `<div class="forecast-point-tooltip__muted">UTC: ${utc.toISOString().replace("T", " ").slice(0, 16)}</div>`,
      `<div class="forecast-point-tooltip__muted">Local: ${local.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</div>`,
    ].join("");

    const pad = 12;
    const maxX = wrap.clientWidth - 200;
    const maxY = wrap.clientHeight - 120;
    setTooltip({
      x: Math.min(maxX, Math.max(pad, param.point.x + pad)),
      y: Math.min(maxY, Math.max(pad, param.point.y - 8)),
      html,
    });
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
      grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
      height: 420,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 12 },
      crosshair: { mode: 1, vertLine: { width: 1, style: LineStyle.Dashed, color: "#4a5a78" } },
      handleScroll: { horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });

    const candles = chart.addCandlestickSeries({
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
    });
    const forecastLine = chart.addLineSeries({
      color: FORECAST_COLOR,
      lineWidth: 2,
      lineStyle: LineStyle.LargeDashed,
      crosshairMarkerVisible: true,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    candleRef.current = candles;
    lineRef.current = forecastLine;

    const onRange = () => {
      if (!suppressRangeRef.current) userRangeRef.current = true;
      layoutForecastChrome();
    };
    chart.timeScale().subscribeVisibleTimeRangeChange(onRange);
    chart.subscribeCrosshairMove(handleCrosshairMove);

    const ro = new ResizeObserver(() => {
      const node = containerRef.current;
      if (!node) return;
      chart.applyOptions({ width: node.clientWidth });
      layoutForecastChrome();
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeVisibleTimeRangeChange(onRange);
      chart.unsubscribeCrosshairMove(handleCrosshairMove);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      lineRef.current = null;
    };
  }, [handleCrosshairMove, layoutForecastChrome]);

  useEffect(() => {
    layoutForecastChrome();
  }, [meta, layoutForecastChrome, tooltip]);

  useEffect(() => {
    userRangeRef.current = false;
  }, [refreshKey]);

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

        const hasForecast =
          data.anchor_price != null &&
          data.anchor_time &&
          data.forecast_points?.length === 6 &&
          data.anchor_price > 0;

        if (hasForecast) {
          activeForecastRef.current = {
            anchorPrice: data.anchor_price!,
            anchorTimeIso: data.anchor_time!,
            points: data.forecast_points,
            runId: data.run_id ?? null,
          };
          const path = buildForecastPath(data.anchor_time!, data.anchor_price!, data.forecast_points);
          lineRef.current.setData(
            path.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }))
          );
          lineRef.current.setMarkers(
            data.forecast_points.map((p) => ({
              time: toUtcSec(p.time) as UTCTimestamp,
              position: "inBar" as const,
              color: FORECAST_COLOR,
              shape: "circle" as const,
              size: 1,
            }))
          );

          const runKey = data.run_id ?? data.generated_at ?? "";
          const shouldSetRange = !userRangeRef.current || runKey !== lastAppliedRunRef.current;
          if (shouldSetRange) {
            const range = computeInitialVisibleRange(candles, data.anchor_time!, data.forecast_points);
            if (range) {
              suppressRangeRef.current = true;
              chartRef.current.timeScale().setVisibleRange({
                from: range.from as UTCTimestamp,
                to: range.to as UTCTimestamp,
              });
              requestAnimationFrame(() => {
                suppressRangeRef.current = false;
              });
              lastAppliedRunRef.current = runKey;
            }
          }
          layoutForecastChrome();
        } else {
          activeForecastRef.current = null;
          anchorTimeRef.current = null;
          lineRef.current.setData([]);
          lineRef.current.setMarkers([]);
          setTooltip(null);
          if (!userRangeRef.current) {
            chartRef.current.timeScale().fitContent();
          }
          layoutForecastChrome();
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
  }, [refreshKey, layoutForecastChrome]);

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
        <div className="forecast-boundary-line" ref={boundaryRef} aria-hidden />
        <div className="forecast-zone-label" ref={zoneLabelRef}>
          AI FORECAST
        </div>
        {tooltip && (
          <div
            ref={tooltipRef}
            className="forecast-point-tooltip"
            style={{ left: tooltip.x, top: tooltip.y }}
            dangerouslySetInnerHTML={{ __html: tooltip.html }}
          />
        )}
        <div className="chart-wrap forecast-chart-canvas" ref={containerRef} />
      </div>
      {meta?.collector_freshness && (
        <p className="hint" style={{ fontSize: "0.8rem" }}>
          وضعیت داده: {Object.entries(meta.collector_freshness).map(([k, v]) => `${k}:${v}`).join(" · ")}
        </p>
      )}
      <p style={{ color: "#9fb0d0", fontSize: "0.82rem" }}>
        خط فیروزه‌ای خط‌چین فقط پیش‌بینی AI است — کندل‌های آینده ساخته نمی‌شوند.
      </p>
    </div>
  );
}
