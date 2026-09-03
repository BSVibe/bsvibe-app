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
  PendingRead,
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

/** Same degradation, but to `null` (UNREAD) instead of []. For a queue whose
 *  EMPTINESS the founder acts on — "nothing is waiting on me" — [] would
 *  launder a read failure into that answer. Non-API errors still propagate. */
function unreadOnApiError<T>(error: unknown): T[] | null {
  if (error instanceof ApiError || error instanceof TypeError) return null;
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

/** Read all three queues in parallel and return the merged Pending list plus
 *  whether the read was COMPLETE.
 *
 *  A single queue failing still degrades to empty rather than blanking the
 *  surface — but it reports `incomplete`, because an empty needs-you list is
 *  not a neutral value: the founder reads it as "nothing is waiting on me" and
 *  `NeedsYou` removes the section entirely. Measured 2026-09-03 with
 *  `/safemode/queue` at 500 and a checkpoint present: the section rendered, the
 *  chip said "1", and a held delivery awaiting approval was invisible with no
 *  trace at all.
 *
 *  ⚠️ Only the three QUEUES set the flag. The review-context reads below omit
 *  no item — they supply the task title + product beside the bare question — so
 *  raising it for them would fire the notice on a degradation the founder
 *  cannot act on, and it would stop meaning "something is missing". */
export async function listPendingDecisions(): Promise<PendingRead> {
  const [deliveries, checkpoints, proposals, runs, deliverables, products] = await Promise.all([
    listSafeModeQueue().catch(unreadOnApiError<SafeModeItem>),
    listCheckpoints().catch(unreadOnApiError<Checkpoint>),
    listPendingProposals().catch(unreadOnApiError<Proposal>),
    // The review-context join — same three reads the Brief already does. Each
    // degrades to empty so a blip just falls back to the bare question.
    listRuns(_RUN_WINDOW).catch(emptyOnApiError<Run>),
    listDeliverables(_RUN_WINDOW).catch(emptyOnApiError<Deliverable>),
    listProducts().catch(emptyOnApiError<Product>),
  ]);
  const lookup = buildReviewLookup(runs, deliverables, products);
  return {
    items: toPendingDecisions(deliveries ?? [], checkpoints ?? [], proposals ?? [], lookup),
    incomplete: deliveries === null || checkpoints === null || proposals === null,
  };
}
