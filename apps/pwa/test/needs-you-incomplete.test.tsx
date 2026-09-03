/**
 * "Needs you" says when it could not read the whole list.
 *
 * `listPendingDecisions` reads three independent queues (held deliveries,
 * paused-run checkpoints, canon proposals) and degrades each to [] on its own
 * blip so one failure never blanks the Brief. That is right — but an EMPTY
 * needs-you list is itself an answer the founder acts on, and `NeedsYou`
 * removes the whole section when the list is empty.
 *
 * Measured live-in-unit 2026-09-03, both halves of the defect:
 *   A (partial) /safemode/queue 500 + one checkpoint → the section rendered,
 *     the chip said "1", and a held delivery awaiting approval was invisible
 *     with NO trace on screen. The screen looked healthy and authoritative.
 *   B (total)   all three 500 → the section vanished entirely, `placeholder`
 *     stayed false, so the founder saw "nothing needs you" while a delivery
 *     waited for their approval.
 *
 * These go through `BriefContent`, not `NeedsYou` directly: the leaf can be
 * handed prop combinations the parent never passes, which is exactly how the
 * unit suite stayed green while the founder-visible behaviour was wrong (#879).
 */

import BriefContent from "@/components/brief/BriefContent";
import type { BriefView, PendingDecision } from "@/lib/api/types";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

function view(over: Partial<BriefView> = {}): BriefView {
  return {
    needsYou: [],
    working: [],
    stream: [],
    placeholder: false,
    hasLiveWorker: true,
    hasProducts: true,
    needsYouIncomplete: false,
    ...over,
  };
}

const NOW = new Date().toISOString();

/** A paused-run checkpoint needs-you item (same shape `brief.test.tsx` uses). */
const checkpoint: PendingDecision = {
  kind: "decision",
  id: "checkpoint-cp-1",
  checkpointId: "cp-1",
  question: "Ship to prod or staging?",
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

describe("Needs you — an unread queue is not an empty queue", () => {
  it("A: says the list is incomplete while still showing what it DID read", () => {
    render(<BriefContent view={view({ needsYou: [checkpoint], needsYouIncomplete: true })} />);

    const section = within(screen.getByRole("region", { name: /needs you/i }));
    // What we measured is still there — an unknown never hides a known.
    expect(section.getByText(/ship to prod or staging/i)).toBeInTheDocument();
    // ...and the screen no longer claims this is the whole list.
    expect(section.getByText(/couldn't be loaded/i)).toBeInTheDocument();
  });

  it("B: keeps the section (with the notice) even when NOTHING could be read", () => {
    // This is the case that used to vanish without a trace.
    render(<BriefContent view={view({ needsYou: [], needsYouIncomplete: true })} />);

    const section = within(screen.getByRole("region", { name: /needs you/i }));
    expect(section.getByText(/couldn't be loaded/i)).toBeInTheDocument();
  });

  it("control: a COMPLETE empty read still hides the section entirely", () => {
    // Without this, deleting the `items.length === 0` early-return would pass
    // the two tests above while leaving an empty amber section on every Brief.
    render(<BriefContent view={view({ needsYou: [], needsYouIncomplete: false })} />);

    expect(screen.queryByRole("region", { name: /needs you/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn't be loaded/i)).not.toBeInTheDocument();
  });

  it("control: a COMPLETE read with items shows no notice", () => {
    render(<BriefContent view={view({ needsYou: [checkpoint], needsYouIncomplete: false })} />);

    const section = within(screen.getByRole("region", { name: /needs you/i }));
    expect(section.getByText(/ship to prod or staging/i)).toBeInTheDocument();
    expect(section.queryByText(/couldn't be loaded/i)).not.toBeInTheDocument();
  });

  it("an incomplete read does NOT suppress the first-run onboarding block", () => {
    // The mirror defect. A pending proposal is NOT run-derived (`Proposal` has
    // no run_id — canon proposals arise from knowledge routes that mint no
    // run), so an unread queue COULD in principle flip `needsYou.length === 0`.
    // Gating `firstRun` on it would strand a genuinely new founder — the exact
    // blocker this screen exists to close, re-created backwards (#879). The
    // honest answer is to make the unknown VISIBLE (the notice above), not to
    // withdraw the guidance.
    render(
      <BriefContent
        view={view({ hasProducts: false, hasLiveWorker: false, needsYouIncomplete: true })}
      />,
    );

    expect(screen.getByText(/create your first product/i)).toBeInTheDocument();
    expect(screen.getByText(/couldn't be loaded/i)).toBeInTheDocument();
  });
});
