import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ForecastChart } from "./ForecastChart";

const mockSetVisibleLogicalRange = vi.fn();
const mockSetVisibleRange = vi.fn();
const mockSetData = vi.fn();
const mockSetMarkers = vi.fn();
const mockSubscribeVisibleTimeRangeChange = vi.fn();
const mockUnsubscribeVisibleTimeRangeChange = vi.fn();
const mockSubscribeVisibleLogicalRangeChange = vi.fn();
const mockUnsubscribeVisibleLogicalRangeChange = vi.fn();
const mockSubscribeCrosshairMove = vi.fn();
const mockUnsubscribeCrosshairMove = vi.fn();
const mockTimeToCoordinate = vi.fn(() => 280);
const mockPriceToCoordinate = vi.fn(() => 200);
const mockTimeScaleWidth = vi.fn(() => 640);
const mockTimeScaleHeight = vi.fn(() => 28);
const mockPriceScaleWidth = vi.fn(() => 56);

vi.mock("lightweight-charts", () => ({
  createChart: vi.fn(() => ({
    addCandlestickSeries: vi.fn(() => ({ setData: mockSetData })),
    addLineSeries: vi.fn(() => ({
      setData: mockSetData,
      setMarkers: mockSetMarkers,
      priceToCoordinate: mockPriceToCoordinate,
    })),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    priceScale: vi.fn(() => ({ width: mockPriceScaleWidth })),
    timeScale: vi.fn(() => ({
      setVisibleRange: mockSetVisibleRange,
      setVisibleLogicalRange: mockSetVisibleLogicalRange,
      fitContent: vi.fn(),
      timeToCoordinate: mockTimeToCoordinate,
      width: mockTimeScaleWidth,
      height: mockTimeScaleHeight,
      subscribeVisibleTimeRangeChange: mockSubscribeVisibleTimeRangeChange,
      unsubscribeVisibleTimeRangeChange: mockUnsubscribeVisibleTimeRangeChange,
      subscribeVisibleLogicalRangeChange: mockSubscribeVisibleLogicalRangeChange,
      unsubscribeVisibleLogicalRangeChange: mockUnsubscribeVisibleLogicalRangeChange,
    })),
    subscribeCrosshairMove: mockSubscribeCrosshairMove,
    unsubscribeCrosshairMove: mockUnsubscribeCrosshairMove,
  })),
  LineStyle: { Dashed: 2, LargeDashed: 3, Solid: 0 },
}));

const sampleChart = () => {
  const anchor = "2026-06-01T10:30:00Z";
  const anchorSec = Math.floor(new Date(anchor).getTime() / 1000);
  const candles = Array.from({ length: 120 }, (_, i) => ({
    time_ms: (anchorSec - (119 - i) * 3600) * 1000,
    open: 80000,
    high: 80100,
    low: 79900,
    close: 80000,
  }));
  const forecast_points = [11, 12, 13, 14, 15, 16].map((hour, i) => ({
    time: `2026-06-01T${String(hour).padStart(2, "0")}:00:00Z`,
    price: 80000 + (i + 1) * 15,
  }));
  return {
    candles,
    anchor_price: 80000,
    anchor_time: anchor,
    forecast_points,
    generated_at: anchor,
    run_id: "run-test-1",
  };
};

vi.mock("./api", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "./api";

describe("ForecastChart", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "setInterval").mockReturnValue(0 as unknown as ReturnType<typeof setInterval>);
    vi.stubGlobal(
      "ResizeObserver",
      vi.fn(() => ({
        observe: vi.fn(),
        disconnect: vi.fn(),
        unobserve: vi.fn(),
      })),
    );
    vi.mocked(apiFetch).mockResolvedValue(sampleChart());
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders overlay layer above canvas and uses logical visible range", async () => {
    const { container } = render(<ForecastChart refreshKey={0} />);
    expect(await screen.findByText("AI FORECAST")).toBeTruthy();
    expect(container.querySelector(".forecast-chart-overlays")).toBeTruthy();
    expect(container.querySelector(".forecast-chart-canvas")).toBeTruthy();
    await waitFor(() => expect(mockSetVisibleLogicalRange).toHaveBeenCalled());
    expect(mockSetVisibleRange).not.toHaveBeenCalled();
    const logical = mockSetVisibleLogicalRange.mock.calls[0][0];
    expect(logical.to - logical.from).toBeGreaterThan(10);
    const markers = mockSetMarkers.mock.calls[0][0];
    expect(markers).toHaveLength(6);
    expect(markers.every((m: { text?: string }) => !m.text)).toBe(true);
  });

  it("handles empty forecast without crashing", async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      candles: [],
      anchor_price: null,
      anchor_time: null,
      forecast_points: [],
      generated_at: null,
    });
    const { container } = render(<ForecastChart />);
    expect(await screen.findByRole("heading", { name: /مسیر پیش‌بینی/ })).toBeTruthy();
    const label = container.querySelector(".forecast-zone-label") as HTMLElement;
    expect(label.style.opacity).toBe("0");
  });
});
