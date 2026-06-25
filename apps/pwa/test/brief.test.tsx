import BriefContent from "@/components/brief/BriefContent";
import type { BriefView } from "@/lib/api/types";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const VIEW: BriefView = {
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
    {
      runId: "r-ship",
      title: "getRelatedPosts function",
      productSlug: "bsvibe-site",
      status: "shipped",
      updatedAt: new Date(Date.now() - 2 * 3600_000).toISOString(),
      deliverableId: "d1",
      artifactType: "pr",
    },
    {
      runId: "r-review",
      title: "Add the export endpoint",
      productSlug: "acme-corp",
      status: "review_ready",
      updatedAt: new Date(Date.now() - 1 * 3600_000).toISOString(),
      deliverableId: "d2",
      artifactType: "doc",
    },
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

describe("Brief (Work Home) surface", () => {
  it("shows the two merged sections: Working on now + Work stream", () => {
    render(<BriefContent view={VIEW} />);
    expect(screen.getByRole("region", { name: "Working on now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Work stream" })).toBeInTheDocument();
  });

  it("does NOT duplicate the Decisions tab — no 'Needs you' decision block on the Brief (#6)", () => {
    render(<BriefContent view={VIEW} />);
    // The dedicated NeedsYou strip (its own region + empty/clear copy) is gone;
    // decisions live in their own tab.
    expect(screen.queryByRole("region", { name: "Needs you" })).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing needs you right now.")).not.toBeInTheDocument();
  });

  it("makes active work the hero — title, product, and a live status", () => {
    render(<BriefContent view={VIEW} />);
    const hero = screen.getByRole("region", { name: "Working on now" });
    expect(
      within(hero).getByText("Writing tests for the related-posts feature"),
    ).toBeInTheDocument();
    expect(within(hero).getByText("Working")).toBeInTheDocument();
    expect(within(hero).getByText("bsvibe-site")).toBeInTheDocument();
  });

  it("links each stream row's title to its report (deliverable) or run", () => {
    render(<BriefContent view={VIEW} />);
    const stream = screen.getByRole("region", { name: "Work stream" });
    // The row TITLE is the link (consistent with the Decisions rows) — a shipped
    // row opens its Delivery Report, the failed row (no deliverable) falls back
    // to opening the run.
    expect(within(stream).getByRole("link", { name: /getRelatedPosts function/ })).toHaveAttribute(
      "href",
      "/deliverables/d1",
    );
    expect(within(stream).getByRole("link", { name: /Broken link fix/ })).toHaveAttribute(
      "href",
      "/runs/r-fail",
    );
  });

  it("deep-links a review_ready stream row to its Decision (#10)", () => {
    render(<BriefContent view={VIEW} />);
    const stream = screen.getByRole("region", { name: "Work stream" });
    // The review_ready row carries a "Review →" link to the Decisions tab so
    // "needs review" isn't a dead end. Non-review rows do NOT.
    const review = within(stream).getByRole("link", { name: /Review/ });
    expect(review).toHaveAttribute("href", "/decisions");
    // Exactly one — the shipped + failed rows have no review link.
    expect(within(stream).getAllByRole("link", { name: /Review/ })).toHaveLength(1);
  });

  it("filters the stream by outcome", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<BriefContent view={VIEW} />);
    const stream = screen.getByRole("region", { name: "Work stream" });
    // "Shipped" filter hides the failed row.
    await userEvent.click(within(stream).getByRole("tab", { name: "Shipped" }));
    expect(within(stream).getByText("getRelatedPosts function")).toBeInTheDocument();
    expect(within(stream).queryByText("Broken link fix")).not.toBeInTheDocument();
  });

  it("shows a calm 'all caught up' when nothing is running", () => {
    render(<BriefContent view={{ ...VIEW, working: [] }} />);
    expect(screen.getByText(/All caught up/)).toBeInTheDocument();
  });
});
