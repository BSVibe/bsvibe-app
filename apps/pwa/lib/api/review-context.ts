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
    const title = summaryTitle || run?.intent || null;
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
