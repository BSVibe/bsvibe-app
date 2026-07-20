/**
 * Unified pending-decisions aggregator — the data behind the Brief's "Needs
 * you" hero, the SINGLE place for everything that genuinely needs the founder's
 * judgment. Rather than read one queue, it folds three EXISTING backend queues
 * into one calm list (no new backend, no change to any endpoint's behaviour —
 * see backend/api/v1/{safemode,checkpoints,decisions}.py):
 *
 *   - "delivery"  ← GET /api/v1/safemode/queue   (held outbound deliveries)
 *   - "decision"  ← GET /api/v1/checkpoints       (paused-run questions)
 *   - "knowledge" ← GET /api/v1/decisions?status_filter=pending  (canon proposals)
 *
 * All three pending kinds (deliveries + checkpoints + proposals) are judged
 * inline in the Brief — there is no separate Decisions tab.
 *
 * Each list degrades to empty on its own per-surface 4xx / network blip so one
 * failing queue never blanks the whole surface (same calm-fallback rule the
 * Brief uses). The merged list is newest-first across kinds.
 */

import { listCheckpoints } from "./checkpoints";
import { ApiError } from "./client";
import { listPendingProposals } from "./decisions";
import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
import { type ReviewLookup, buildReviewLookup } from "./review-context";
import { listRuns } from "./runs";
import { listSafeModeQueue } from "./safemode";
import type {
  Checkpoint,
  Deliverable,
  PendingDecision,
  Product,
  Proposal,
  Run,
  SafeModeItem,
} from "./types";

const _RUN_WINDOW = 50;

/** Swallow a per-surface ApiError / network blip into an empty list so one
 *  failing queue does not blank the whole "Needs you" surface. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}

/** Map the three raw queue responses → the unified, kind-tagged Pending list,
 *  newest-first across kinds. */
export function toPendingDecisions(
  deliveries: SafeModeItem[],
  checkpoints: Checkpoint[],
  proposals: Proposal[],
  lookup?: ReviewLookup,
): PendingDecision[] {
  const items: PendingDecision[] = [];
  for (const d of deliveries) {
    // Join the run/deliverable so the row says WHAT is being shipped and links
    // to its proof, instead of a blind generic "a delivery is held".
    const ctx = lookup?.forDelivery(d.deliverable_id, d.run_id ?? null);
    items.push({
      kind: "delivery",
      id: `delivery-${d.id}`,
      itemId: d.id,
      runId: d.run_id ?? null,
      deliverableId: d.deliverable_id,
      title: ctx?.title ?? null,
      productSlug: ctx?.productSlug,
      detailHref: ctx?.detailHref ?? null,
      createdAt: d.created_at,
    });
  }
  for (const c of checkpoints) {
    const ctx = lookup?.forRun(c.run_id);
    items.push({
      kind: "decision",
      id: `checkpoint-${c.id}`,
      checkpointId: c.id,
      question: c.question,
      runId: c.run_id,
      title: ctx?.title ?? null,
      productSlug: ctx?.productSlug,
      detailHref: ctx?.detailHref ?? null,
      // L-D1 — LLM-suggested options. Null/empty falls back to free-text;
      // CheckpointRow always renders an "Other" radio so the founder isn't
      // locked into the suggested set.
      options: c.options && c.options.length > 0 ? c.options : null,
      // L-D2 — one-click action specs (ship / discard) on executor B2b
      // Decisions. When non-empty the row renders dedicated action buttons.
      actions: c.actions && c.actions.length > 0 ? c.actions : null,
      decision: c.decision,
      rationale: c.rationale,
      // G4 — prior resolved decisions the founder can answer consistently with.
      priorDecisions: c.prior_decisions ?? [],
      createdAt: c.created_at,
    });
  }
  for (const p of proposals) {
    items.push({
      kind: "knowledge",
      id: `proposal-${p.id}`,
      proposal: p,
      createdAt: p.created_at,
    });
  }
  // Newest-first across kinds. Items with an unparseable timestamp sort last.
  return items.sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}

/** Read all three queues in parallel and return the merged Pending list. A
 *  single optional queue failing degrades to empty rather than blanking the
 *  surface. */
export async function listPendingDecisions(): Promise<PendingDecision[]> {
  const [deliveries, checkpoints, proposals, runs, deliverables, products] = await Promise.all([
    listSafeModeQueue().catch(emptyOnApiError<SafeModeItem>),
    listCheckpoints().catch(emptyOnApiError<Checkpoint>),
    listPendingProposals().catch(emptyOnApiError<Proposal>),
    // The review-context join — same three reads the Brief already does. Each
    // degrades to empty so a blip just falls back to the bare question.
    listRuns(_RUN_WINDOW).catch(emptyOnApiError<Run>),
    listDeliverables(_RUN_WINDOW).catch(emptyOnApiError<Deliverable>),
    listProducts().catch(emptyOnApiError<Product>),
  ]);
  const lookup = buildReviewLookup(runs, deliverables, products);
  return toPendingDecisions(deliveries, checkpoints, proposals, lookup);
}
