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
  computeInitialVisibleLogicalRange,
  findNearestForecastPointByPixel,
  forecastHourLabel,
  formatUsd,
  clampTooltipInPlotArea,
  forecastRunKey,
  isNewForecastRun,
  isXInsidePlot,
  pctChangeFromAnchor,
  plotAreaInsetStyle,
  shouldApplyForecastVisibleRange,
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

type TooltipState = {
  x: number;
  y: number;
  pointIndex: number;
  point: ForecastPoint;
  anchorPrice: number;
};

const FORECAST_COLOR = "#00e5ff";
const TOOLTIP_HIT_PX = 22;

export function ForecastChart({ refreshKey = 0 }: { refreshKey?: number }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlaysRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const boundaryRef = useRef<HTMLDivElement | null>(null);
  const zoneLabelRef = useRef<HTMLDivElement | null>(null);

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
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);

  const layoutForecastChrome = useCallback(() => {
    const chart = chartRef.current;
    const wrap = wrapRef.current;
    const overlays = overlaysRef.current;
    const overlay = overlayRef.current;
    const boundary = boundaryRef.current;
    const label = zoneLabelRef.current;
    const anchorTime = anchorTimeRef.current;

    if (!chart || !wrap || !overlays || !overlay || !boundary || !label) return;

    const plotWidth = chart.timeScale().width();
    const timeScaleHeight = chart.timeScale().height();
    const priceScaleWidth = chart.priceScale("right").width();

    const inset = plotAreaInsetStyle(priceScaleWidth, timeScaleHeight);
    overlays.style.top = inset.top;
    overlays.style.left = inset.left;
    overlays.style.right = inset.right;
    overlays.style.bottom = inset.bottom;
    overlays.style.padding = "0";

    if (!anchorTime || plotWidth <= 0) {
      overlay.style.width = "0px";
      boundary.style.opacity = "0";
      label.style.opacity = "0";
      return;
    }

    const anchorSec = toUtcSec(anchorTime) as UTCTimestamp;
    const anchorX = chart.timeScale().timeToCoordinate(anchorSec);

    if (!isXInsidePlot(anchorX, plotWidth)) {
      overlay.style.width = "0px";
      boundary.style.opacity = "0";
      label.style.opacity = "0";
      return;
    }

    const left = anchorX;
    overlay.style.left = `${left}px`;
    const plotAreaWidth = overlays.clientWidth;
    overlay.style.width = `${Math.max(0, plotAreaWidth - left)}px`;
    boundary.style.left = `${left}px`;
    boundary.style.opacity = "1";
    label.style.left = `${left + 10}px`;
    label.style.opacity = "1";
  }, []);

  const handleCrosshairMove = useCallback((param: MouseEventParams<Time>) => {
    const overlays = overlaysRef.current;
    const chart = chartRef.current;
    const line = lineRef.current;
    const forecast = activeForecastRef.current;

    if (!overlays || !chart || !line || !forecast || !param.point) {
      setTooltip(null);
      return;
    }

    const coords = forecast.points.map((p) => ({
      x: chart.timeScale().timeToCoordinate(toUtcSec(p.time) as UTCTimestamp),
      y: line.priceToCoordinate(p.price),
    }));

    const idx = findNearestForecastPointByPixel(
      forecast.points,
      coords,
      param.point.x,
      param.point.y,
      TOOLTIP_HIT_PX,
    );

    if (idx === null) {
      setTooltip(null);
      return;
    }

    const pt = forecast.points[idx];
    const offset = clampTooltipInPlotArea(
      param.point.x + 12,
      param.point.y - 8,
      overlays.clientWidth,
      overlays.clientHeight,
    );
    setTooltip({
      pointIndex: idx,
      point: pt,
      anchorPrice: forecast.anchorPrice,
      x: offset.x,
      y: offset.y,
    });
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: { background: { color: "#0c1224" }, textColor: "#d5def5" },
      grid: { vertLines: { color: "#1b2640" }, horzLines: { color: "#1b2640" } },
      height: 420,
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 12 },
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
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
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
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
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
    lastAppliedRunRef.current = null;
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
          lineRef.current.setData(path.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
          lineRef.current.setMarkers(
            data.forecast_points.map((p) => ({
              time: toUtcSec(p.time) as UTCTimestamp,
              position: "inBar" as const,
              color: FORECAST_COLOR,
              shape: "circle" as const,
              size: 1,
            }))
          );

          const runKey = forecastRunKey(data);
          const isNewRun = isNewForecastRun(runKey, lastAppliedRunRef.current);
          const shouldSetRange = shouldApplyForecastVisibleRange({
            isPoll,
            userAdjustedRange: userRangeRef.current,
            isNewRun,
            hasForecast: true,
          });

          if (shouldSetRange) {
            const logical = computeInitialVisibleLogicalRange(candles.length);
            if (logical) {
              suppressRangeRef.current = true;
              chartRef.current.timeScale().setVisibleLogicalRange(logical);
              requestAnimationFrame(() => {
                suppressRangeRef.current = false;
                layoutForecastChrome();
              });
              lastAppliedRunRef.current = runKey;
              userRangeRef.current = false;
            }
          }
          layoutForecastChrome();
        } else {
          activeForecastRef.current = null;
          anchorTimeRef.current = null;
          lineRef.current.setData([]);
          lineRef.current.setMarkers([]);
          setTooltip(null);
          if (!isPoll && !userRangeRef.current) {
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
        <div className="forecast-chart-canvas chart-wrap" ref={containerRef} />
        <div className="forecast-chart-overlays" ref={overlaysRef}>
          <div className="forecast-future-shade" ref={overlayRef} aria-hidden />
          <div className="forecast-boundary-line" ref={boundaryRef} aria-hidden />
          <div className="forecast-zone-label" ref={zoneLabelRef}>
            AI FORECAST
          </div>
          {tooltip && (
            <div className="forecast-point-tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
              <div className="forecast-point-tooltip__title">
                {forecastHourLabel(tooltip.pointIndex)} — AI Forecast
              </div>
              <div className="forecast-point-tooltip__row">
                Predicted: <strong>{formatUsd(tooltip.point.price)}</strong>
              </div>
              <div className="forecast-point-tooltip__row">
                Change from Anchor:{" "}
                <strong>{pctChangeFromAnchor(tooltip.point.price, tooltip.anchorPrice)}</strong>
              </div>
              <div className="forecast-point-tooltip__muted">
                UTC: {new Date(tooltip.point.time).toISOString().replace("T", " ").slice(0, 16)}
              </div>
              <div className="forecast-point-tooltip__muted">
                Local:{" "}
                {new Date(tooltip.point.time).toLocaleString(undefined, {
                  dateStyle: "medium",
                  timeStyle: "short",
                })}
              </div>
            </div>
          )}
        </div>
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
