import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StrategyDiagnostics } from "./StrategyDiagnostics";

describe("strategy persistence diagnostics", () => {
  it("distinguishes created, missing and blocked plans", () => {
    render(<StrategyDiagnostics structured={{_artifacts: {strategy_persistence: {
      ict_reversal: {created: true, setup_id: "setup-a", lifecycle_state: "WAIT_LONG"},
      trend_continuation: {skipped: true, reason: "candidate_not_reported"},
      breakout_retest: {skipped: true, reason: "parallel_same_direction_use_revision"},
    }}}} />);
    expect(screen.getByText(/فرصت ثبت شد/)).toBeTruthy();
    expect(screen.getByText(/مدل پیشنهاد مستقل/)).toBeTruthy();
    expect(screen.getByText(/ستاپ هم‌جهت همین روش/)).toBeTruthy();
    expect(screen.getByText("setup-a")).toBeTruthy();
  });
  it("does not invent results for older runs", () => {
    const { container } = render(<StrategyDiagnostics structured={{}} />);
    expect(container.textContent).toBe("");
  });
});
