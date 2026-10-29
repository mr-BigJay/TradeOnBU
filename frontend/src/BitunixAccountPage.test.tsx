import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { BitunixAccountPage } from "./BitunixAccountPage";
import { apiFetch } from "./api";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));

const sample = {
  configured: true, read_only: true, live_trading: false, updated_at: "2026-10-29T19:00:00Z",
  symbol: "BTCUSDT",
  account: {
    currency: "USDT", available: "100", frozen: "8", position_margin: "30",
    unrealized_pnl: "3", cross_unrealized_pnl: "0", isolated_unrealized_pnl: "3",
    transferable: "80", bonus: "0", position_mode: "HEDGE",
  },
  settings: { leverage: "10", margin_mode: "ISOLATION" },
  positions: [{
    id: "123", side: "LONG", leverage: "5", margin_mode: "ISOLATION",
    qty: "0.001", entry_price: "80000", margin: "20", entry_value: "80",
    unrealized_pnl: "3", realized_pnl: "0", roi_pct: "15",
    liq_price: "70000", margin_rate: "0.05", fee: "0.1", funding: "-0.1", created_at: null,
  }],
  orders: [{
    id: "456", side: "SELL", type: "LIMIT", status: "NEW_", qty: "0.003",
    filled_qty: "0", price: "82703.1", leverage: "5", margin_mode: "ISOLATION",
    reduce_only: false, tp_price: null, sl_price: null, created_at: null,
  }],
  tpsl_orders: [], history: { positions: [], displayed: 0, total: 0 },
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset().mockResolvedValue(sample);
});
afterEach(cleanup);

it("displays live account margin and distinct position and symbol leverage", async () => {
  render(<BitunixAccountPage />);
  expect(await screen.findByText("حساب واقعی Bitunix")).toBeInTheDocument();
  expect(await screen.findByText("موجودی آزاد")).toBeInTheDocument();
  expect(screen.getByText("مارجین پوزیشن‌ها")).toBeInTheDocument();
  expect(screen.getByText("پوزیشن‌های باز")).toBeInTheDocument();
  expect(screen.getByText("سفارش‌های در انتظار")).toBeInTheDocument();
  expect(screen.getByText("پوزیشن‌های بسته‌شده")).toBeInTheDocument();
  expect(screen.getByText("سفارش‌های TP / SL مستقل")).toBeInTheDocument();
  expect(screen.getByText("HEDGE")).toBeInTheDocument();
  expect(screen.getByText("10×")).toBeInTheDocument();
  expect(screen.getAllByText("5×").length).toBeGreaterThan(0);
  expect(apiFetch).toHaveBeenCalledWith("/api/gui/bitunix/snapshot");
  fireEvent.click(screen.getByRole("button", { name: "به‌روزرسانی حساب" }));
  await waitFor(() => expect(vi.mocked(apiFetch).mock.calls.length).toBeGreaterThanOrEqual(2));
  expect(vi.mocked(apiFetch).mock.calls.every(([p]) => p === "/api/gui/bitunix/snapshot")).toBe(true);
});

it("shows unconfigured credentials without asking for them in the browser", async () => {
  vi.mocked(apiFetch).mockResolvedValue({ configured: false, read_only: true, live_trading: false });
  render(<BitunixAccountPage />);
  expect(await screen.findByText("اتصال دائمی API تنظیم نشده")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("on API failure does not display stale balances or trading controls", async () => {
  vi.mocked(apiFetch).mockRejectedValue(new Error("API unavailable"));
  render(<BitunixAccountPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
  expect(screen.queryByText("مارجین پوزیشن‌ها")).toBeNull();
  expect(screen.queryByText("ثبت سفارش")).toBeNull();
});
