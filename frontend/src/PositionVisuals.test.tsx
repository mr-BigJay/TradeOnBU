import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EquityCurve, MoneyValue, PositionTradeCard, numberText } from "./PositionVisuals";

describe("Position financial display", () => {
  it("formats signed PnL with tabular LTR numbers and correct semantic colors", () => {
    const { container, rerender } = render(<MoneyValue value={42.7} signed />);
    expect(screen.getByText("+42.70")).toBeInTheDocument();
    expect(container.querySelector(".financial-value--profit")).toHaveAttribute("dir", "ltr");
    rerender(<MoneyValue value={-18.35} signed />);
    expect(screen.getByText("-18.35")).toBeInTheDocument();
    expect(container.querySelector(".financial-value--loss")).toBeInTheDocument();
    rerender(<MoneyValue value={0} signed />);
    expect(container.querySelector(".financial-value--neutral")).toBeInTheDocument();
    expect(screen.getByText("0.00")).toBeInTheDocument();
  });

  it("never invents values when the quote is unavailable", () => {
    const { container } = render(<MoneyValue value={null} signed />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(container.querySelector(".financial-value--profit")).toBeNull();
    expect(numberText(Number.NaN)).toBe("—");
    expect(numberText(undefined)).toBe("—");
  });

  it("keeps LONG/SHORT direction separate from profit/loss indication", () => {
    const cancel = vi.fn();
    const close = vi.fn();
    const { container } = render(
      <PositionTradeCard
        position={{
          public_id: "p-1", symbol: "BTCUSDT", direction: "SHORT",
          status: "open", order_type: "limit", leverage: 10, margin_usdt: 50,
          entry_price: 100, stop_loss: 105, take_profit: 90, mark_price: 101,
          metrics: { net_unrealized_pnl: -3.25, net_rr: 2.1 },
        }}
        onCancel={cancel} onClose={close}
      />,
    );
    expect(container.querySelector(".position-direction--short")).toBeInTheDocument();
    expect(container.querySelector(".financial-value--loss")).toBeInTheDocument();
    expect(screen.getByText("-3.25")).toBeInTheDocument();
    expect(screen.getByText("1 : 2.10")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "بستن دستی" }));
    expect(close).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
  });

  it("pending entry has no fabricated unrealized PnL and retains cancellation", () => {
    const cancel = vi.fn();
    const { container } = render(
      <PositionTradeCard position={{
        public_id: "p-2", direction: "LONG", status: "pending_entry",
        leverage: 10, margin_usdt: 50, order_type: "limit",
        entry_price: 100, stop_loss: 95, take_profit: 110,
        metrics: { net_unrealized_pnl: 99 },
      }} onCancel={cancel} onClose={vi.fn()} />,
    );
    expect(container.querySelector(".position-trade-card__pnl")).toHaveTextContent("—");
    expect(container.querySelector(".position-trade-card__pnl")).not.toHaveTextContent("99");
    fireEvent.click(screen.getByRole("button", { name: "لغو سفارش" }));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("shows empty-state when no real equity snapshots exist", () => {
    render(<EquityCurve history={[]} />);
    expect(screen.getByText("هنوز داده کافی برای نمودار موجودی وجود ندارد")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("charts only real finite API snapshots", () => {
    render(<EquityCurve history={[
      { equity_usdt: 1000, recorded_at_tehran: "10:00" },
      { equity_usdt: NaN, recorded_at_tehran: "10:01" },
      { equity_usdt: 1020, recorded_at_tehran: "10:05" },
    ]} />);
    expect(screen.getByRole("img", { name: /روند اکوییتی/ })).toBeInTheDocument();
    expect(screen.getByText("+20.00")).toBeInTheDocument();
  });
});
