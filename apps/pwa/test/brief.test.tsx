import BriefContent from "@/components/brief/BriefContent";
import type { BriefView, PendingDecision } from "@/lib/api/types";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// The needs-you rows reuse the real DeliveryRow / CheckpointRow, which call the
// safemode / checkpoints clients on resolve. Stub them so the inline resolve
// succeeds (and onResolved fires) without a live backend.
vi.mock("@/lib/api/safemode", () => ({
  approveSafeModeItem: vi.fn(async () => ({})),
  denySafeModeItem: vi.fn(async () => ({})),
}));
vi.mock("@/lib/api/checkpoints", () => ({
  resolveCheckpoint: vi.fn(async () => ({})),
  resolveCheckpointAction: vi.fn(async () => ({})),
}));

const NOW = new Date().toISOString();

/** A held Safe-Mode delivery needs-you item (resolves via Approve / Decline). */
function delivery(id: string, title: string): PendingDecision {
  return {
    kind: "delivery",
    id: `delivery-${id}`,
    itemId: id,
    runId: "r-d",
    deliverableId: "d-held",
    title,
    productSlug: "bsvibe-site",
    detailHref: "/deliverables/d-held",
    createdAt: NOW,
  };
}

/** A paused-run checkpoint needs-you item with LLM options + an "Other" path. */
function checkpoint(id: string, question: string): PendingDecision {
  return {
    kind: "decision",
    id: `checkpoint-${id}`,
    checkpointId: id,
    question,
    options: ["prod", "staging"],
    actions: null,
    decision: "ask_user_question",
    rationale: null,
    priorDecisions: [],
    runId: "r-c",
    title: "Build the export endpoint",
    productSlug: "acme-corp",
    detailHref: "/runs/r-c",
    createdAt: NOW,
  };
}

function shipped(runId: string, title: string, hoursAgo: number): BriefView["stream"][number] {
  return {
    runId,
    title,
    productSlug: "bsvibe-site",
    status: "shipped",
    updatedAt: new Date(Date.now() - hoursAgo * 3600_000).toISOString(),
    deliverableId: `del-${runId}`,
    artifactType: "pr",
  };
}

const VIEW: BriefView = {
  needsYou: [
    delivery("sm-1", "Send the launch email"),
    checkpoint("cp-1", "Ship to prod or staging?"),
  ],
  working: [
    {
      runId: "r-active",
      title: "Writing tests for the related-posts feature",
      productSlug: "bsvibe-site",
      status: "running",
      startedAt: new Date(Date.now() - 4 * 60_000).toISOString(),
    },
  ],
  stream: [
    shipped("r-ship", "getRelatedPosts function", 2),
    {
      runId: "r-fail",
      title: "Broken link fix",
      productSlug: "nexus-portal",
      status: "failed",
      updatedAt: new Date(Date.now() - 26 * 3600_000).toISOString(),
      deliverableId: null,
      artifactType: null,
    },
  ],
  placeholder: false,
};

describe("Brief (unified Work-Home + Decisions) surface", () => {
  it("renders the Needs-you, Working, and Shipped sections", () => {
    render(<BriefContent view={VIEW} />);
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Working on now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Shipped" })).toBeInTheDocument();
  });

  it("R4: renders the needs-you decisions INLINE (reusing CheckpointRow + DeliveryRow)", () => {
    render(<BriefContent view={VIEW} />);
    const needs = screen.getByRole("region", { name: "Needs you" });
    // The held delivery row — Approve / Decline are present in place (DeliveryRow).
    expect(within(needs).getByText("Send the launch email")).toBeInTheDocument();
    // The checkpoint card — its question + the LLM-offered options rendered as
    // selectable CHIPS, plus a persistent "type your own" input (CheckpointRow).
    expect(within(needs).getByText("Ship to prod or staging?")).toBeInTheDocument();
    expect(within(needs).getByRole("button", { name: "prod" })).toBeInTheDocument();
    expect(within(needs).getByRole("button", { name: "staging" })).toBeInTheDocument();
    // The contextual amber status badge replaces the old generic kind chip.
    expect(within(needs).getByText("Needs your answer")).toBeInTheDocument();
    // Both action shapes stack as separate cards.
    expect(within(needs).getByText("Approve")).toBeInTheDocument();
  });

  it("R4: resolving a needs-you item re-reads the Brief (onResolved wired through)", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const onResolved = vi.fn();
    render(<BriefContent view={VIEW} onNeedsYouResolved={onResolved} />);
    const needs = screen.getByRole("region", { name: "Needs you" });
    await userEvent.click(within(needs).getByText("Approve"));
    expect(onResolved).toHaveBeenCalled();
  });

  it("makes active work a hero — title, product, and a live status", () => {
    render(<BriefContent view={VIEW} />);
    const hero = screen.getByRole("region", { name: "Working on now" });
    expect(
      within(hero).getByText("Writing tests for the related-posts feature"),
    ).toBeInTheDocument();
    expect(within(hero).getByText("Working")).toBeInTheDocument();
  });

  it("R4: collapses Shipped to a count + View-all (NOT an endless list)", () => {
    // 9 shipped rows but the collapsed view shows at most the recent few.
    const many: BriefView = {
      ...VIEW,
      stream: [
        ...Array.from({ length: 9 }, (_, i) => shipped(`s${i}`, `Shipped item ${i}`, i + 1)),
        VIEW.stream[1], // the failed row stays out of the shipped count
      ],
    };
    render(<BriefContent view={many} />);
    const shippedRegion = screen.getByRole("region", { name: "Shipped" });
    // The header carries a total count.
    expect(within(shippedRegion).getByText(/9/)).toBeInTheDocument();
    // Only a handful render collapsed — the oldest is hidden until View all.
    expect(within(shippedRegion).queryByText("Shipped item 8")).not.toBeInTheDocument();
    expect(within(shippedRegion).getByText("Shipped item 0")).toBeInTheDocument();
    // A View-all affordance exists.
    expect(within(shippedRegion).getByRole("button", { name: /View all/ })).toBeInTheDocument();
  });

  it("R4: View all expands the full shipped history", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const many: BriefView = {
      ...VIEW,
      stream: Array.from({ length: 9 }, (_, i) => shipped(`s${i}`, `Shipped item ${i}`, i + 1)),
    };
    render(<BriefContent view={many} />);
    const shippedRegion = screen.getByRole("region", { name: "Shipped" });
    await userEvent.click(within(shippedRegion).getByRole("button", { name: /View all/ }));
    expect(within(shippedRegion).getByText("Shipped item 8")).toBeInTheDocument();
  });

  it("R4: each shipped row keeps its link to its report", () => {
    render(<BriefContent view={VIEW} />);
    const shippedRegion = screen.getByRole("region", { name: "Shipped" });
    expect(
      within(shippedRegion).getByRole("link", { name: /getRelatedPosts function/ }),
    ).toHaveAttribute("href", "/deliverables/del-r-ship");
  });

  it("R4: filter chips narrow the visible sections", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<BriefContent view={VIEW} />);
    const chips = screen.getByRole("tablist", { name: /filter/i });
    // "Needs you N" chip — only the needs-you section remains.
    await userEvent.click(within(chips).getByRole("tab", { name: /Needs you/ }));
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Working on now" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Shipped" })).not.toBeInTheDocument();

    // "Working" chip — only the working section.
    await userEvent.click(within(chips).getByRole("tab", { name: "Working" }));
    expect(screen.getByRole("region", { name: "Working on now" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();

    // "All" chip — everything back.
    await userEvent.click(within(chips).getByRole("tab", { name: "All" }));
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Working on now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Shipped" })).toBeInTheDocument();
  });

  it("R4: the Shipped filter shows the full shipped list", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const many: BriefView = {
      ...VIEW,
      stream: Array.from({ length: 9 }, (_, i) => shipped(`s${i}`, `Shipped item ${i}`, i + 1)),
    };
    render(<BriefContent view={many} />);
    const chips = screen.getByRole("tablist", { name: /filter/i });
    await userEvent.click(within(chips).getByRole("tab", { name: "Shipped" }));
    const shippedRegion = screen.getByRole("region", { name: "Shipped" });
    // Full list (the otherwise-collapsed oldest row is visible).
    expect(within(shippedRegion).getByText("Shipped item 8")).toBeInTheDocument();
  });

  it("shows a calm 'all caught up' when nothing is running", () => {
    render(<BriefContent view={{ ...VIEW, working: [] }} />);
    expect(screen.getByText(/All caught up/)).toBeInTheDocument();
  });

  it("the Needs-you chip carries the pending count when there are items", () => {
    render(<BriefContent view={VIEW} />);
    const chips = screen.getByRole("tablist", { name: /filter/i });
    // 2 pending needs-you items → the chip reads "Needs you 2".
    expect(within(chips).getByRole("tab", { name: /Needs you 2/ })).toBeInTheDocument();
  });
});
