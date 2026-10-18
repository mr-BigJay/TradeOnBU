import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PriceForecastPage } from "./PriceForecastPage";

vi.mock("./api", () => ({
  apiFetch: vi.fn(async (path: string) => {
    if (path.includes("/status")) {
      return {
        enabled: false,
        interval_minutes: 60,
        model: "gpt-6-luna",
        reasoning_effort: "low",
        max_output_tokens: 1200,
        daily_request_limit: 48,
        monthly_budget_usd: 5,
        last_run_at: null,
        last_success_at: null,
        next_run_at: null,
        last_error: null,
        running_status: "idle",
        runs_today: 0,
        month_cost_usd: 0,
      };
    }
    if (path.includes("/chart")) {
      return { candles: [], forecast_points: [], anchor_price: null, anchor_time: null, generated_at: null };
    }
    return {};
  }),
}));

vi.mock("./ForecastChart", () => ({
  ForecastChart: () => <div data-testid="forecast-chart" />,
}));

describe("PriceForecastPage", () => {
  it("renders forecast tab content", async () => {
    render(<PriceForecastPage />);
    expect(await screen.findByText("پیش‌بینی قیمت")).toBeTruthy();
    expect(screen.getByText("تولید پیش‌بینی جدید")).toBeTruthy();
    expect(screen.getByTestId("forecast-chart")).toBeTruthy();
  });
});
