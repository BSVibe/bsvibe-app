import DeliveryRow from "@/components/decisions/DeliveryRow";
import ChangeFootprint from "@/components/deliverables/ChangeFootprint";
import { toPendingDecisions } from "@/lib/api/pending";
import type { PendingDelivery, SafeModeItem } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const LARGE = {
  changed_files: 250,
  large_change: true,
  large_change_warning: "큰 변경",
  top_dirs: [
    { dir: ".pnpm-store", count: 240 },
    { dir: "src", count: 10 },
  ],
};

const SMALL = {
  changed_files: 3,
  large_change: false,
  large_change_warning: null,
  top_dirs: [{ dir: "src", count: 3 }],
};

function delivery(change: PendingDelivery["change"]): PendingDelivery {
  return {
    kind: "delivery",
    id: "delivery-1",
    itemId: "item-1",
    runId: "run-1",
    title: "Add export",
    detailHref: "/deliverables/d1",
    change,
    createdAt: "2026-10-29T00:00:00Z",
  };
}

describe("ChangeFootprint", () => {
  it("warns on a large change and lists the busiest directories", () => {
    render(<ChangeFootprint footprint={LARGE} />);
    expect(screen.getByText("250 files changed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Large change");
    expect(screen.getByText(".pnpm-store (240)")).toBeInTheDocument();
    expect(screen.getByText("src (10)")).toBeInTheDocument();
  });

  it("shows the count but no warning for a small change", () => {
    render(<ChangeFootprint footprint={SMALL} />);
    expect(screen.getByText("3 files changed")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Large change/)).toBeNull();
  });

  it("renders nothing when the deliverable declared no artifact_refs", () => {
    const { container } = render(<ChangeFootprint footprint={{ changed_files: null }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Safe Mode card", () => {
  it("shows the warning and top dirs on a large_change card", () => {
    render(<DeliveryRow item={delivery(LARGE)} onResolved={() => {}} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Large change");
    expect(screen.getByText(".pnpm-store (240)")).toBeInTheDocument();
  });

  it("shows no warning on a small deliverable's card", () => {
    render(<DeliveryRow item={delivery(SMALL)} onResolved={() => {}} />);
    expect(screen.getByText("3 files changed")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("carries the queue item's footprint onto the pending delivery", () => {
    const item: SafeModeItem = {
      id: "item-1",
      workspace_id: "ws",
      deliverable_id: "d1",
      run_id: "run-1",
      status: "pending",
      compensation_tier: null,
      expires_at: "2026-11-01T00:00:00Z",
      extension_count: 0,
      created_at: "2026-10-29T00:00:00Z",
      ...LARGE,
    };
    const [pending] = toPendingDecisions([item], [], []);
    expect(pending.kind === "delivery" && pending.change?.large_change).toBe(true);
    expect(pending.kind === "delivery" && pending.change?.changed_files).toBe(250);
  });
});
