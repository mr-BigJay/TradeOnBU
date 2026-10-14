import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { PositionPage, type PositionPrefill } from "./PositionPage";

const apiFetch = vi.fn();

vi.mock("./api", () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}));

describe("PositionPage AI prefill", () => {
  beforeEach(() => {
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

    const selects = await screen.findAllByRole("combobox");
    const orderTypeSelect = selects[1];
    expect(orderTypeSelect).toHaveValue("limit");

    expect(await screen.findByDisplayValue("100")).toBeInTheDocument();
    expect(screen.getByDisplayValue("95")).toBeInTheDocument();
    expect(screen.getByDisplayValue("105")).toBeInTheDocument();
    expect(orderTypeSelect).not.toHaveValue("market");
  });
});
