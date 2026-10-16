import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { PositionPage, type PositionPrefill } from "./PositionPage";

const apiFetch = vi.fn();

vi.mock("./api", () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}));

describe("PositionPage AI prefill", () => {
  beforeEach(() => {
    cleanup();
    apiFetch.mockReset();
    apiFetch.mockImplementation((path: string) => {
      if (path.includes("/settings")) {
        return Promise.resolve({
          default_margin_usdt: 50,
          default_leverage: 10,
          default_order_type: "market",
        });
      }
      if (path.includes("/positions")) {
        return Promise.resolve({ positions: [] });
      }
      if (path.includes("/reports")) {
        return Promise.resolve({
          net_profit_loss: 12.5, account_equity: 1012.5, total_trades: 2,
          win_rate: 50, profit_factor: 1.4, max_drawdown_pct: 2,
          equity_history: [], fees: 1.2, history: [],
        });
      }
      if (path.includes("/wallet")) {
        return Promise.resolve({
          account_equity: 1012.5, wallet_balance: 1000, available_balance: 900,
          margin_in_use: 100, unrealized_pnl: -3.2, realized_pnl: 12.5,
          total_fees: 1.2, initial_balance_set: true, ledger: [],
        });
      }
      return Promise.resolve({});
    });
  });

  it("keeps limit order and AI entry levels after settings load", async () => {
    const prefill: PositionPrefill = {
      direction: "LONG",
      entry_price: 100,
      stop_loss: 95,
      take_profit: 105,
      ai_setup_id: "setup-1",
    };

    render(
      <PositionPage
        prefill={prefill}
        onConsumePrefill={() => {}}
        initialTab="open"
      />,
    );

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith("/api/gui/position/settings");
    });

    const orderTypeSelect = await screen.findByLabelText("نوع ورود");
    expect(orderTypeSelect).toHaveValue("limit");

    expect(await screen.findByDisplayValue("100")).toBeInTheDocument();
    expect(screen.getByDisplayValue("95")).toBeInTheDocument();
    expect(screen.getByDisplayValue("105")).toBeInTheDocument();
    expect(orderTypeSelect).not.toHaveValue("market");
    expect(screen.getByRole("button", { name: "LONG" })).toHaveAttribute("aria-pressed", "true");
  });
  it("defaults to the approved combined dashboard with wallet, positions and 30-day report", async () => {
    apiFetch.mockImplementation((path: string) => {
      if (path.includes("/positions")) {
        return Promise.resolve({ positions: [
          { public_id: "a-1", symbol: "BTCUSDT", direction: "LONG", status: "open",
            entry_price: 100, mark_price: 101, stop_loss: 95, take_profit: 110,
            leverage: 10, margin_usdt: 50, metrics: { net_unrealized_pnl: 5 } },
          { public_id: "p-1", symbol: "BTCUSDT", direction: "SHORT", status: "pending_entry",
            entry_price: 100, stop_loss: 105, take_profit: 90,
            leverage: 10, margin_usdt: 50, metrics: { net_unrealized_pnl: 999 } },
        ] });
      }
      if (path.includes("/reports")) {
        return Promise.resolve({
          net_profit_loss: 12.5, win_rate: 50, profit_factor: 1.4,
          max_drawdown_pct: 2, fees: 1.2, equity_history: [], history: [],
        });
      }
      if (path.includes("/wallet")) {
        return Promise.resolve({
          account_equity: 1012.5, available_balance: 900, margin_in_use: 50,
          unrealized_pnl: 5, realized_pnl: 12.5, total_fees: 1.2,
        });
      }
      return Promise.resolve({ maker_fee_rate: 0.0002, taker_fee_rate: 0.0006 });
    });
    render(<PositionPage prefill={null} onConsumePrefill={() => {}} />);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith("/api/gui/position/reports?period=30d");
    });
    expect(await screen.findByText("خلاصه حساب")).toBeInTheDocument();
    expect(await screen.findByText("پوزیشن‌های باز")).toBeInTheDocument();
    expect(screen.getByText("سفارش‌های در انتظار ورود")).toBeInTheDocument();
    expect(screen.getByText("ثبت سفارش جدید")).toBeInTheDocument();
    expect(await screen.findByText("گزارش عملکرد")).toBeInTheDocument();
    const cards = document.querySelectorAll(".position-trade-card");
    expect(cards).toHaveLength(2);
    expect(cards[1].querySelector(".position-trade-card__pnl")).toHaveTextContent("—");
    expect(cards[1].querySelector(".position-trade-card__pnl")).not.toHaveTextContent("999");
    expect(screen.getByRole("link", { name: "+ سفارش جدید" })).toHaveAttribute("href", "#position-new-order");
    fireEvent.click(screen.getByRole("button", { name: "گزارش کامل" }));
    expect(await screen.findByText("تاریخچه معاملات")).toBeInTheDocument();
  });

});
