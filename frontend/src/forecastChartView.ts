export type ForecastCandle = { time_ms: number; open: number; high: number; low: number; close: number };
export type ForecastPointLike = { time: string; price: number };

/** ~14 hourly candles of history + 6h forecast + padding → ~30% future band. */
export const DEFAULT_HISTORY_CANDLES = 14;
export const FUTURE_RANGE_PADDING_SEC = 3600;

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

export function computeInitialVisibleRange(
  candles: ForecastCandle[],
  anchorTimeIso: string,
  forecastPoints: ForecastPointLike[],
  opts?: { historyCandles?: number; futurePaddingSec?: number },
): { from: number; to: number } | null {
  if (!anchorTimeIso || forecastPoints.length < 6) return null;
  const historyCandles = opts?.historyCandles ?? DEFAULT_HISTORY_CANDLES;
  const futurePaddingSec = opts?.futurePaddingSec ?? FUTURE_RANGE_PADDING_SEC;
  const anchorSec = toUtcSec(anchorTimeIso);
  const lastForecastSec = toUtcSec(forecastPoints[5].time);
  const to = lastForecastSec + futurePaddingSec;

  const candleTimes = candles.map((c) => Math.floor(c.time_ms / 1000)).sort((a, b) => a - b);
  let from: number;
  if (candleTimes.length) {
    let idx = candleTimes.findIndex((t) => t > anchorSec);
    if (idx === -1) idx = candleTimes.length - 1;
    else idx = Math.max(0, idx - 1);
    const startIdx = Math.max(0, idx - (historyCandles - 1));
    from = candleTimes[startIdx];
    const minFrom = anchorSec - historyCandles * 3600;
    from = Math.max(candleTimes[0], Math.min(from, minFrom));
  } else {
    from = anchorSec - historyCandles * 3600;
  }
  if (from >= to) return null;
  return { from, to };
}

/** Match crosshair time to an hourly forecast point (H1–H6). */
export function findForecastPointIndex(
  crosshairTimeSec: number,
  forecastPoints: ForecastPointLike[],
  toleranceSec = 1200,
): number | null {
  for (let i = 0; i < forecastPoints.length; i++) {
    const ptSec = toUtcSec(forecastPoints[i].time);
    if (Math.abs(ptSec - crosshairTimeSec) <= toleranceSec) return i;
  }
  return null;
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
