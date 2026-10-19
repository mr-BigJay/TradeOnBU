import { describe, expect, it } from "vitest";
import {
  buildForecastPath,
  computeInitialVisibleLogicalRange,
  findNearestForecastPointByPixel,
  forecastHourLabel,
  clampTooltipInPlotArea,
  isXInsidePlot,
  pctChangeFromAnchor,
  plotAreaInsetStyle,
} from "./forecastChartView";

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
  });

  it("logical range shows ~14 history bars and future padding past last candle", () => {
    const range = computeInitialVisibleLogicalRange(120)!;
    expect(range.from).toBe(120 - 14);
    expect(range.to).toBe(119 + 6 + 2);
    const visibleBars = range.to - range.from;
    const futureBars = range.to - 119;
    expect(futureBars / visibleBars).toBeGreaterThan(0.25);
  });

  it("pixel hit test prefers nearest marker within radius", () => {
    const points = [
      { time: "2026-06-01T11:00:00Z", price: 1 },
      { time: "2026-06-01T12:00:00Z", price: 1 },
    ];
    const coords = [
      { x: 100, y: 100 },
      { x: 200, y: 100 },
    ];
    expect(findNearestForecastPointByPixel(points, coords, 105, 108, 22)).toBe(0);
    expect(findNearestForecastPointByPixel(points, coords, 150, 100, 22)).toBeNull();
    expect(findNearestForecastPointByPixel(points, coords, 198, 115, 22)).toBe(1);
  });

  it("plot visibility guard for boundary", () => {
    expect(isXInsidePlot(10, 400)).toBe(true);
    expect(isXInsidePlot(-1, 400)).toBe(false);
    expect(isXInsidePlot(401, 400)).toBe(false);
    expect(isXInsidePlot(null, 400)).toBe(false);
  });

  it("plot area inset excludes price and time scales", () => {
    const inset = plotAreaInsetStyle(56, 28);
    expect(inset.right).toBe("56px");
    expect(inset.bottom).toBe("28px");
  });

  it("clamps tooltip inside plot box", () => {
    const out = clampTooltipInPlotArea(500, 400, 640, 392, { tooltipWidth: 220, tooltipHeight: 118, pad: 8 });
    expect(out.x).toBeLessThanOrEqual(640 - 220 - 8);
    expect(out.y).toBeLessThanOrEqual(392 - 118 - 8);
  });

  it("formats labels and pct change", () => {
    expect(forecastHourLabel(2)).toBe("H3");
    expect(pctChangeFromAnchor(100.05, 100)).toBe("+0.050%");
  });
});
