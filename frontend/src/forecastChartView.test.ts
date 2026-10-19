import { describe, expect, it } from "vitest";
import {
  buildForecastPath,
  computeInitialVisibleRange,
  findForecastPointIndex,
  forecastHourLabel,
  pctChangeFromAnchor,
} from "./forecastChartView";

function candle(timeSec: number): { time_ms: number; open: number; high: number; low: number; close: number } {
  return { time_ms: timeSec * 1000, open: 1, high: 1, low: 1, close: 1 };
}

describe("forecastChartView", () => {
  it("builds anchor + six forecast path points", () => {
    const anchor = "2026-06-01T10:30:00Z";
    const fixed = [
      { time: "2026-06-01T11:00:00Z", price: 80010 },
      { time: "2026-06-01T12:00:00Z", price: 80020 },
      { time: "2026-06-01T13:00:00Z", price: 80030 },
      { time: "2026-06-01T14:00:00Z", price: 80040 },
      { time: "2026-06-01T15:00:00Z", price: 80050 },
      { time: "2026-06-01T16:00:00Z", price: 80060 },
    ];
    const path = buildForecastPath(anchor, 80000, fixed);
    expect(path).toHaveLength(7);
    expect(path[0].value).toBe(80000);
    expect(path[6].time).toBe(Math.floor(new Date("2026-06-01T16:00:00Z").getTime() / 1000));
  });

  it("initial visible range leaves ~30% for six forecast hours", () => {
    const anchorSec = Math.floor(new Date("2026-06-01T10:30:00Z").getTime() / 1000);
    const candles = Array.from({ length: 120 }, (_, i) => candle(anchorSec - (119 - i) * 3600));
    const forecast = [
      "2026-06-01T11:00:00Z",
      "2026-06-01T12:00:00Z",
      "2026-06-01T13:00:00Z",
      "2026-06-01T14:00:00Z",
      "2026-06-01T15:00:00Z",
      "2026-06-01T16:00:00Z",
    ].map((time, i) => ({ time, price: 81000 + i * 5 }));

    const range = computeInitialVisibleRange(candles, "2026-06-01T10:30:00Z", forecast)!;
    const span = range.to - range.from;
    const futureSpan = range.to - anchorSec;
    const ratio = futureSpan / span;
    expect(ratio).toBeGreaterThan(0.25);
    expect(ratio).toBeLessThan(0.45);
  });

  it("finds forecast point near crosshair time", () => {
    const points = [{ time: "2026-06-01T11:00:00Z", price: 1 }];
    const t = Math.floor(new Date("2026-06-01T11:00:00Z").getTime() / 1000);
    expect(findForecastPointIndex(t, points)).toBe(0);
    expect(findForecastPointIndex(t + 5000, [{ time: "2026-06-01T12:00:00Z", price: 1 }])).toBeNull();
  });

  it("formats labels and pct change", () => {
    expect(forecastHourLabel(2)).toBe("H3");
    expect(pctChangeFromAnchor(100.05, 100)).toBe("+0.050%");
  });
});
