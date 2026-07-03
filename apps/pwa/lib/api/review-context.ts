/**
 * Shared review-context lookup — the join that lets every "needs your judgment"
 * surface (Decisions rows, Brief "Needs you", product Recent runs) show WHAT a
 * pending item is, concisely, and link to its full proof.
 *
 * The Brief Work Stream already does this join inline (run.intent OR the
 * deliverable's concise summary + a /deliverables/<id> link). The decision /
 * delivery / checkpoint surfaces showed only a generic question ("A delivery is
 * held in Safe Mode. Send it out?") — so the founder approved blind. This folds
 * the same three EXISTING reads (runs + deliverables + products) into one
 * reusable lookup so the final approval reads clearly everywhere, with no
 * backend change.
 */

import { conciseSummary } from "../text/summary";
import type { Deliverable, Product, Run } from "./types";

export interface ReviewContext {
  /** One-line plain title: the deliverable's concise summary, else the run's
   *  Direction (intent), else null when neither is known. */
  title: string | null;
  /** Product slug for the chip; "workspace" when unbound / unknown. */
  productSlug: string;
  deliverableId: string | null;
  runId: string | null;
  /** Where "view" links: the deliverable proof when shipped, else the run, else
   *  null (no detail to open). */
  detailHref: string | null;
}

export interface ReviewLookup {
  /** Context for an item known by its run id (checkpoints). */
  forRun(runId: string | null): ReviewContext;
  /** Context for an item known by its deliverable id (Safe Mode deliveries);
   *  the run id is recovered from the deliverable when not supplied. */
  forDelivery(deliverableId: string | null, runId: string | null): ReviewContext;
}

// F4 — a frame title that DENIES a task was given ("No task provided", "No
// concrete request was provided", "nothing to execute"). The frame LLM emits
// these when it misjudges a real Direction as empty; a produced deliverable
// contradicts them, so they must not stand as a card title.
const DEGENERATE_TITLE =
  /^\s*(no\b.{0,24}\b(task|request|instruction|work|direction)|nothing to (execute|do|build))/i;

/** A frame/intent string usable as a card title — trimmed and non-degenerate,
 *  else null so the caller falls through to the next ground-truth source. */
function usableTitle(text: string | null | undefined): string | null {
  const trimmed = text?.trim();
  if (!trimmed || DEGENERATE_TITLE.test(trimmed)) return null;
  return trimmed;
}

/** Build the lookup once from the three lists, then resolve many items O(1). */
export function buildReviewLookup(
  runs: Run[],
  deliverables: Deliverable[],
  products: Product[],
): ReviewLookup {
  const runById = new Map(runs.map((r) => [r.id, r]));
  const deliverableById = new Map(deliverables.map((d) => [d.id, d]));
  // Newest-first input → first deliverable seen per run is its latest.
  const latestDeliverableByRun = new Map<string, Deliverable>();
  for (const d of deliverables) {
    if (!latestDeliverableByRun.has(d.run_id)) latestDeliverableByRun.set(d.run_id, d);
  }

  function slugOf(productId: string | null | undefined): string {
    if (!productId) return "workspace";
    return products.find((p) => p.id === productId)?.slug ?? "workspace";
  }

  function contextOf(run: Run | undefined, deliverable: Deliverable | undefined): ReviewContext {
    const summaryTitle = deliverable?.summary ? conciseSummary(deliverable.summary, "") : "";
    // L8 — lead with the frame's short, plain-language task title; fall back to
    // the retroactive framed_intent, then the deliverable's concise summary,
    // then the raw (developer-y) Direction. So the founder reads "Add a mean
    // helper", not "In the bsvibe-app product, add `mean(values: list[float])…".
    // F4 — but the frame LLM can misfire, classifying a REAL request as "no
    // task" (summary_title="No task provided"). A produced deliverable proves
    // work happened, so skip a degenerate "no task" frame title and fall
    // through to the founder's real Direction rather than tell them nothing
    // was asked. A genuinely empty request still degrades to the generic card.
    const title =
      usableTitle(run?.summary_title) ||
      usableTitle(run?.framed_intent) ||
      summaryTitle ||
      usableTitle(run?.intent) ||
      null;
    const deliverableId = deliverable?.id ?? null;
    const runId = run?.id ?? deliverable?.run_id ?? null;
    const detailHref = deliverableId
      ? `/deliverables/${deliverableId}`
      : runId
        ? `/runs/${runId}`
        : null;
    return { title, productSlug: slugOf(run?.product_id), deliverableId, runId, detailHref };
  }

  return {
    forRun(runId) {
      const run = runId ? runById.get(runId) : undefined;
      const deliverable = runId ? latestDeliverableByRun.get(runId) : undefined;
      return contextOf(run, deliverable);
    },
    forDelivery(deliverableId, runId) {
      const deliverable = deliverableId ? deliverableById.get(deliverableId) : undefined;
      const resolvedRunId = runId ?? deliverable?.run_id ?? null;
      const run = resolvedRunId ? runById.get(resolvedRunId) : undefined;
      return contextOf(run, deliverable);
    },
  };
}
