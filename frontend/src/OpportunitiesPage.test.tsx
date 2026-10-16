import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OpportunitiesPage } from "./OpportunitiesPage";

const apiFetch = vi.fn();

vi.mock("./api", () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}));

function listPayload(tab: string, label: string, total = 1) {
  return {
    tab,
    items: [
      {
        entity_type: "setup",
        setup_id: `${tab}-only`,
        symbol: "BTCUSDT",
        direction: "LONG",
        lifecycle_state: label,
        status_label_fa: label,
      },
    ],
    total,
    page: 1,
    page_size: 20,
    total_pages: 1,
  };
}

describe("OpportunitiesPage", () => {
  beforeEach(() => {
    cleanup();
    apiFetch.mockReset();
    apiFetch.mockImplementation((path: string) => {
      if (path.includes("/summary")) {
        return Promise.resolve({ counts: { wait: 1, ready: 1, blocked: 0, expired: 0 } });
      }
      if (path.includes("tab=wait")) {
        return new Promise((resolve) => {
          setTimeout(() => resolve(listPayload("wait", "WAIT_LONG")), 80);
        });
      }
      if (path.includes("tab=ready")) {
        return Promise.resolve(listPayload("ready", "READY_LONG"));
      }
      if (path.includes("tab=paper")) {
        const pending = path.includes("paper_status=pending");
        return Promise.resolve({
          tab: "paper",
          paper_status: pending ? "pending" : "open",
          items: pending
            ? [
                {
                  entity_type: "paper_position",
                  public_id: "paper-1",
                  symbol: "BTCUSDT",
                  direction: "LONG",
                  status: "pending_entry",
                  paper_segment: "pending",
                },
              ]
            : [],
          total: pending ? 1 : 0,
          page: 1,
          page_size: 20,
          total_pages: 1,
        });
      }
      return Promise.resolve(listPayload("other", "PENDING_ENTRY"));
    });
  });

  it("does not show stale tab rows after fast tab switch (race guard)", async () => {
    render(<OpportunitiesPage />);
    fireEvent.click(screen.getByRole("tab", { name: "آماده اجرا" }));
    await waitFor(() => expect(screen.getAllByText("READY_LONG").length).toBeGreaterThan(0));
    expect(screen.queryByText("WAIT_LONG")).not.toBeInTheDocument();
  });

  it("keeps independent page keys per tab when paginating", async () => {
    apiFetch.mockImplementation((path: string) => {
      if (path.includes("/summary")) {
        return Promise.resolve({ counts: { wait: 40, ready: 0, blocked: 0, expired: 0 } });
      }
      if (path.includes("tab=wait")) {
        const page = path.includes("page=2") ? 2 : 1;
        return Promise.resolve({
          tab: "wait",
          items: [{ entity_type: "setup", setup_id: `w-${page}`, symbol: "X", direction: "LONG", lifecycle_state: "WAIT_LONG", status_label_fa: "w" }],
          total: 40,
          page,
          page_size: 20,
          total_pages: 2,
        });
      }
      if (path.includes("tab=ready")) {
        return Promise.resolve(listPayload("ready", "READY_LONG", 0));
      }
      return Promise.resolve(listPayload("wait", "WAIT_LONG"));
    });

    render(<OpportunitiesPage />);
    await waitFor(() => expect(screen.getByText(/1–20 از 40/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "بعدی" }));
    await waitFor(() => expect(screen.getByText(/21–40 از 40/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: "آماده اجرا" }));
    await waitFor(() => expect(screen.getAllByText("READY_LONG").length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("tab", { name: "در انتظار تأیید" }));
    await waitFor(() => expect(screen.getByText(/21–40 از 40/)).toBeInTheDocument());
  });

  it("resets paper segment to page 1 and loads pending segment", async () => {
    render(<OpportunitiesPage />);
    fireEvent.click(screen.getByRole("tab", { name: "Paper" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining("paper_status=pending")));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining("paper_status=open")));
  });

  it("includes responsive layout hooks for mobile and desktop", async () => {
    const { container } = render(<OpportunitiesPage />);
    expect(container.querySelector(".opp-tabs-scroll")).toBeTruthy();
    await waitFor(() => {
      expect(container.querySelector(".opp-summary-grid")).toBeTruthy();
      expect(document.querySelector(".desktop-only")).toBeTruthy();
      expect(document.querySelector(".mobile-only")).toBeTruthy();
    });
  });
});
