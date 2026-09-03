import BriefContent from "@/components/brief/BriefContent";
import OnboardingChecklist from "@/components/brief/OnboardingChecklist";
import WorkingNow from "@/components/brief/WorkingNow";
import type { ActiveWork, BriefView } from "@/lib/api/types";
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
    ...over,
  };
}

const activeRun: ActiveWork = {
  runId: "r1",
  title: "Build the export endpoint",
  productSlug: "acme",
  status: "running",
  startedAt: new Date(Date.now() - 5 * 60_000).toISOString(),
};

describe("Brief onboarding + honest worker status", () => {
  it("shows the onboarding checklist on a brand-new workspace (no products, no runs)", () => {
    render(<BriefContent view={view({ hasProducts: false, hasLiveWorker: false })} />);
    // The three first-value steps are surfaced.
    expect(screen.getByText(/create your first product/i)).toBeInTheDocument();
    expect(screen.getByText(/connect a worker/i)).toBeInTheDocument();
    expect(screen.getByText(/send your first request/i)).toBeInTheDocument();
  });

  it("does NOT show the checklist once the workspace has products + a live worker", () => {
    render(<BriefContent view={view({ hasProducts: true, hasLiveWorker: true })} />);
    expect(screen.queryByText(/create your first product/i)).not.toBeInTheDocument();
  });

  // Measured live on a fresh stack (2026-09-03): creating a product made the
  // WHOLE block vanish, so the product step's ✓ — which `OnboardingChecklist`
  // renders and `docs/e2e/pwa-onboarding-checklist.md` promises — was a state
  // production could never reach. The parent hid on `hasProducts` alone while
  // the component's own contract is "hide once the workspace can actually
  // produce", and producing needs a worker.
  //
  // ⚠️ This goes through `BriefContent`, not `OnboardingChecklist` directly.
  // The direct-render test below can show any prop combination it likes,
  // including ones the parent never passes — which is exactly why the unit
  // suite was green while the founder-visible behaviour was broken.
  it("keeps the checklist (product step ✓) when a product exists but no worker is connected", () => {
    render(<BriefContent view={view({ hasProducts: true, hasLiveWorker: false })} />);

    const productStep = screen.getByText(/create your first product/i).closest("li");
    expect(productStep?.className).toMatch(/done/);
    // The steps the founder still has to do must survive — losing them is how
    // a new founder gets stranded one step short of first value.
    expect(screen.getByText(/connect a worker/i)).toBeInTheDocument();
  });

  // The checklist doc promises this step "deep-links to Settings → Models →
  // Executor workers (the register/service install surface)". `/settings` is a
  // redirect to the GENERAL tab, so linking there drops the founder one tab
  // away from the only screen that can finish the step — measured live
  // 2026-09-03, the click landed on `/settings/general`.
  it("worker step deep-links to the Models tab, where the executor-worker surface lives", () => {
    render(<OnboardingChecklist hasProducts={false} hasLiveWorker={false} />);

    const link = screen.getByRole("link", { name: /set up a worker/i });
    expect(link).toHaveAttribute("href", "/settings/models");
  });

  it("checklist marks the worker step done when a live worker is connected", () => {
    render(<OnboardingChecklist hasProducts={false} hasLiveWorker={true} />);
    // step 2 (connect a worker) is checked off
    const workerStep = screen.getByText(/connect a worker/i).closest("li");
    expect(workerStep?.className).toMatch(/done/);
  });

  it("WorkingNow shows an honest 'waiting for a worker' state when no live worker", () => {
    render(<WorkingNow items={[activeRun]} hasLiveWorker={false} />);
    expect(screen.getByText(/waiting for a worker/i)).toBeInTheDocument();
    // and does NOT present the run as actively 'Working'
    expect(screen.queryByText(/^Working$/)).not.toBeInTheDocument();
  });

  it("WorkingNow shows the normal working state when a worker IS live", () => {
    render(<WorkingNow items={[activeRun]} hasLiveWorker={true} />);
    expect(screen.queryByText(/waiting for a worker/i)).not.toBeInTheDocument();
    // The status pill (exact "Working"), not the "Working on now" heading.
    expect(screen.getByText("Working")).toBeInTheDocument();
  });
  /* ── A `/workers` blip must not be spoken as a measurement ────────────────
   * `getBrief` degrades a failed /workers read to `[]`, which used to reach the
   * Brief as `hasLiveWorker: false` — indistinguishable from "we asked and
   * nothing is live". Two founder-visible claims are keyed off it, so one blip
   * asserted two things nobody measured. `null` = unknown; neither claim may be
   * made from it. Both tests go through BriefContent, not the leaf components,
   * because the leaf can be handed prop combinations the parent never passes. */

  it("does NOT revive onboarding when a product exists and the worker read FAILED (unknown)", () => {
    render(<BriefContent view={view({ hasProducts: true, hasLiveWorker: null })} />);
    // "You cannot produce yet" is a claim; an unknown worker cannot support it.
    expect(screen.queryByText(/create your first product/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/connect a worker/i)).not.toBeInTheDocument();
  });

  it("still shows onboarding on a brand-new workspace even when the worker read FAILED", () => {
    // Here the unknown does not matter: with no product the workspace cannot
    // produce whatever the worker answer would have been. Suppressing the block
    // on any unknown would strand the founder this screen exists to onboard.
    render(<BriefContent view={view({ hasProducts: false, hasLiveWorker: null })} />);
    expect(screen.getByText(/create your first product/i)).toBeInTheDocument();
  });

  it("does NOT call active runs 'waiting for a worker' when the worker read FAILED", () => {
    render(<BriefContent view={view({ hasLiveWorker: null, working: [activeRun] })} />);
    // Scoped to the hero section — the filter chip row also says "Working".
    const hero = within(screen.getByRole("region", { name: /working on now/i }));
    expect(hero.queryByText(/waiting for a worker/i)).not.toBeInTheDocument();
    expect(hero.getByText("Working")).toBeInTheDocument();
  });

  it("control: a MEASURED absence of a live worker still says 'waiting for a worker'", () => {
    render(<BriefContent view={view({ hasLiveWorker: false, working: [activeRun] })} />);
    expect(screen.getByText(/waiting for a worker/i)).toBeInTheDocument();
  });
});
