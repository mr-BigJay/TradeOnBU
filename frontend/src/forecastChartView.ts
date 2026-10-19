export type ForecastCandle = { time_ms: number; open: number; high: number; low: number; close: number };
export type ForecastPointLike = { time: string; price: number };

/** ~14 hourly candles of history + 6 forecast bars + padding on logical scale. */
export const DEFAULT_HISTORY_CANDLES = 14;
export const FUTURE_FORECAST_BARS = 6;
export const FUTURE_LOGICAL_PADDING_BARS = 2;

export function toUtcSec(iso: string): number {
  return Math.floor(new Date(iso).getTime() / 1000);
}

export function forecastHourLabel(index: number): string {
  return `H${index + 1}`;
}

export function pctChangeFromAnchor(price: number, anchorPrice: number): string {
  if (!anchorPrice || !Number.isFinite(anchorPrice)) return "—";
  const pct = ((price - anchorPrice) / anchorPrice) * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(3)}%`;
}

export function formatUsd(price: number): string {
  return `$${price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * Logical range for lightweight-charts: extends past last candle without fake timestamps.
 * `to` includes ~6 forecast hourly bars plus padding so H6 is not glued to the right edge.
 */
export function computeInitialVisibleLogicalRange(
  candleCount: number,
  opts?: { historyCandles?: number; futureBars?: number; paddingBars?: number },
): { from: number; to: number } | null {
  if (candleCount <= 0) return null;
  const historyCandles = opts?.historyCandles ?? DEFAULT_HISTORY_CANDLES;
  const futureBars = opts?.futureBars ?? FUTURE_FORECAST_BARS;
  const paddingBars = opts?.paddingBars ?? FUTURE_LOGICAL_PADDING_BARS;
  const lastIdx = candleCount - 1;
  return {
    from: Math.max(0, lastIdx - (historyCandles - 1)),
    to: lastIdx + futureBars + paddingBars,
  };
}

export function isXInsidePlot(x: number | null | undefined, plotWidth: number): x is number {
  return x != null && x >= 0 && x <= plotWidth;
}

/** @deprecated time-based visible range cannot extrapolate past last bar in LWC v4 */
export function computeInitialVisibleRange(
  candles: ForecastCandle[],
  anchorTimeIso: string,
  forecastPoints: ForecastPointLike[],
): { from: number; to: number } | null {
  void candles;
  void anchorTimeIso;
  void forecastPoints;
  return null;
}

/** Pixel hit test: show tooltip only when crosshair is near the marker (not just same time). */
export function findNearestForecastPointByPixel(
  points: ForecastPointLike[],
  coords: { x: number | null; y: number | null }[],
  crosshairX: number,
  crosshairY: number,
  maxDistancePx = 22,
): number | null {
  let best: number | null = null;
  let bestDist = maxDistancePx;
  for (let i = 0; i < points.length; i++) {
    const x = coords[i]?.x;
    const y = coords[i]?.y;
    if (x == null || y == null) continue;
    const dx = crosshairX - x;
    const dy = crosshairY - y;
    const d = Math.hypot(dx, dy);
    if (d <= bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

export function buildForecastPath(
  anchorTimeIso: string,
  anchorPrice: number,
  forecastPoints: ForecastPointLike[],
): { time: number; value: number }[] {
  const anchorSec = toUtcSec(anchorTimeIso);
  return [
    { time: anchorSec, value: anchorPrice },
    ...forecastPoints.map((p) => ({ time: toUtcSec(p.time), value: p.price })),
  ];
}
