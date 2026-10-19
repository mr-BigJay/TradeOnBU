import { describe, expect, it } from "vitest";
import {
  forecastRunKey,
  isNewForecastRun,
  shouldApplyForecastVisibleRange,
} from "./forecastChartView";

describe("forecast visible range policy", () => {
  it("poll with same run_id preserves user zoom", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: true,
        userAdjustedRange: true,
        isNewRun: false,
        hasForecast: true,
      }),
    ).toBe(false);
  });

  it("poll with new run_id recenters even if user zoomed", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: true,
        userAdjustedRange: true,
        isNewRun: true,
        hasForecast: true,
      }),
    ).toBe(true);
  });

  it("initial load recenters when user has not zoomed", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: false,
        userAdjustedRange: false,
        isNewRun: false,
        hasForecast: true,
      }),
    ).toBe(true);
  });

  it("manual refresh after user zoom keeps range unless new run", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: false,
        userAdjustedRange: true,
        isNewRun: false,
        hasForecast: true,
      }),
    ).toBe(false);
  });

  it("manual generate / new run always recenters", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: false,
        userAdjustedRange: true,
        isNewRun: true,
        hasForecast: true,
      }),
    ).toBe(true);
  });

  it("no forecast never sets range", () => {
    expect(
      shouldApplyForecastVisibleRange({
        isPoll: false,
        userAdjustedRange: false,
        isNewRun: true,
        hasForecast: false,
      }),
    ).toBe(false);
  });

  it("run key helpers detect scheduler poll updates", () => {
    const a = forecastRunKey({ run_id: "run-1", generated_at: "t1" });
    const b = forecastRunKey({ run_id: "run-2", generated_at: "t1" });
    expect(isNewForecastRun(a, null)).toBe(true);
    expect(isNewForecastRun(a, a)).toBe(false);
    expect(isNewForecastRun(b, a)).toBe(true);
  });
});
