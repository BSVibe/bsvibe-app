/**
 * Settings → Plan (#928 step 4). Reads GET /api/v1/workspace/plan and shows the plan, each
 * limit and how much of it is in use. A `null` limit is UNLIMITED — rendering it as 0 would
 * tell an uncapped workspace it has nothing.
 */

import PlanTab from "@/components/settings/PlanTab";
import type { WorkspacePlan } from "@/lib/api/workspace";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getWorkspacePlan = vi.fn<() => Promise<WorkspacePlan>>();
const setWorkspaceTokenBudget = vi.fn<(limit: number | null) => Promise<WorkspacePlan>>();

vi.mock("@/lib/api/workspace", () => ({
  getWorkspacePlan: () => getWorkspacePlan(),
  setWorkspaceTokenBudget: (limit: number | null) => setWorkspaceTokenBudget(limit),
}));

function plan(overrides: Partial<WorkspacePlan> = {}): WorkspacePlan {
  return {
    plan: "free",
    runs: { held: 1, limit: 3 },
    tokens: { used: 3_500, limit: 50_000_000, period_start: "2026-10-01T00:00:00+00:00" },
    verify_stack_slots: 1,
    billing_status: null,
    ...overrides,
  };
}

beforeEach(() => {
  getWorkspacePlan.mockReset();
  setWorkspaceTokenBudget.mockReset();
});

describe("Settings → Plan", () => {
  it("names the plan and shows each limit beside its use", async () => {
    getWorkspacePlan.mockResolvedValue(plan());
    render(<PlanTab />);

    expect(await screen.findByText("Free")).toBeInTheDocument();
    expect(screen.getByLabelText("Concurrent runs")).toHaveTextContent("1 / 3");
    expect(screen.getByLabelText("Tokens this month")).toHaveTextContent("3,500 / 50,000,000");
    expect(screen.getByLabelText("Verification stacks")).toHaveTextContent("1");
  });

  it("shows an unlimited limit as Unlimited, never as zero", async () => {
    getWorkspacePlan.mockResolvedValue(
      plan({
        plan: "operator",
        runs: { held: 4, limit: null },
        tokens: { used: 10, limit: null, period_start: "2026-10-01T00:00:00+00:00" },
      }),
    );
    render(<PlanTab />);

    expect(await screen.findByLabelText("Concurrent runs")).toHaveTextContent("4 / Unlimited");
    expect(screen.getByLabelText("Tokens this month")).toHaveTextContent("10 / Unlimited");
    expect(screen.getByLabelText("Concurrent runs")).not.toHaveTextContent("/ 0");
  });

  it("says a payment is past due while the plan still holds", async () => {
    getWorkspacePlan.mockResolvedValue(plan({ plan: "pro", billing_status: "past_due" }));
    render(<PlanTab />);

    expect(await screen.findByText("Pro")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/past due/i);
  });

  it("points the free plan at the paid-plan page without promising an upgrade", async () => {
    // Founder decision 2026-10-16: billing is paused and BSVibe launches free. The pricing page
    // says paid plans are coming — a button saying "Upgrade" would promise what it cannot do.
    getWorkspacePlan.mockResolvedValue(plan());
    const { unmount } = render(<PlanTab />);
    expect(await screen.findByRole("link", { name: /paid plans/i })).toBeInTheDocument();
    expect(screen.queryByText(/upgrade/i)).toBeNull();
    unmount();

    getWorkspacePlan.mockResolvedValue(plan({ plan: "pro" }));
    render(<PlanTab />);
    await screen.findByText("Pro");
    expect(screen.queryByRole("link", { name: /paid plans/i })).toBeNull();
  });

  it("shows an empty budget as no limit and saves a chosen one", async () => {
    getWorkspacePlan.mockResolvedValue(
      plan({ tokens: { used: 10, limit: null, period_start: "2026-10-01T00:00:00+00:00" } }),
    );
    setWorkspaceTokenBudget.mockResolvedValue(
      plan({ tokens: { used: 10, limit: 5_000, period_start: "2026-10-01T00:00:00+00:00" } }),
    );
    render(<PlanTab />);

    const field = await screen.findByLabelText("Monthly token budget", { selector: "input" });
    expect(field).toHaveValue("");
    expect(screen.getByLabelText("Tokens this month")).toHaveTextContent("10 / Unlimited");
    expect(screen.queryByRole("button", { name: "Remove limit" })).toBeNull();

    fireEvent.change(field, { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget" }));

    await waitFor(() => expect(setWorkspaceTokenBudget).toHaveBeenCalledWith(5_000));
    expect(await screen.findByRole("button", { name: "Remove limit" })).toBeInTheDocument();
  });

  it("clears a set budget and rejects a non-positive value", async () => {
    getWorkspacePlan.mockResolvedValue(plan());
    setWorkspaceTokenBudget.mockResolvedValue(
      plan({ tokens: { used: 3_500, limit: null, period_start: "2026-10-01T00:00:00+00:00" } }),
    );
    render(<PlanTab />);

    const field = await screen.findByLabelText("Monthly token budget", { selector: "input" });
    fireEvent.change(field, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/greater than 0/i);
    expect(setWorkspaceTokenBudget).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Remove limit" }));
    await waitFor(() => expect(setWorkspaceTokenBudget).toHaveBeenCalledWith(null));
  });

  it("fails calmly when the plan cannot be read", async () => {
    getWorkspacePlan.mockRejectedValue(new Error("boom"));
    render(<PlanTab />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t load/i);
  });
});
