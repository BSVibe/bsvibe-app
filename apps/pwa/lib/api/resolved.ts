/**
 * Unified resolved-decisions aggregator — the history side of {@link
 * ./pending.ts}.
 *
 * The Decisions "Resolved" tab folds the SAME three queues' settled items into
 * one calm list (no new endpoint behaviour — see
 * backend/api/v1/{safemode,checkpoints,decisions}.py):
 *
 *   - "delivery"  ← GET /api/v1/safemode/resolved   (decided held deliveries)
 *   - "decision"  ← GET /api/v1/checkpoints/resolved (answered paused-run questions)
 *   - "knowledge" ← GET /api/v1/decisions/log        (canon decision audit trail)
 *
 * Before this, the Resolved tab read ONLY the canon log, so resolved Safe-Mode
 * deliveries + answered checkpoints vanished after the founder acted on them.
 * Each list degrades to empty on its own per-surface 4xx / network blip so one
 * failing queue never blanks the surface. Merged newest-resolved first.
 */

import { listResolvedCheckpoints } from "./checkpoints";
import { ApiError } from "./client";
import { listDecisionsLog } from "./decisions";
import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
import { type ReviewLookup, buildReviewLookup } from "./review-context";
import { listRuns } from "./runs";
import { listResolvedSafeMode } from "./safemode";
import type {
  DecisionLogEntry,
  Deliverable,
  Product,
  ResolvedCheckpointItem,
  ResolvedDecision,
  Run,
  SafeModeResolvedItem,
} from "./types";

const _RUN_WINDOW = 50;

/** Swallow a per-surface ApiError / network blip into an empty list so one
 *  failing queue does not blank the whole Resolved tab. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}

/** Map the three raw resolved responses → the unified, kind-tagged Resolved
 *  list, newest-resolved first across kinds. */
export function toResolvedDecisions(
  deliveries: SafeModeResolvedItem[],
  checkpoints: ResolvedCheckpointItem[],
  log: DecisionLogEntry[],
  lookup?: ReviewLookup,
): ResolvedDecision[] {
  const items: ResolvedDecision[] = [];
  for (const d of deliveries) {
    // Join the run/deliverable so the resolved row says WHAT was decided and
    // links to its proof — the same context the PENDING delivery row carries —
    // instead of a blind generic "delivery approved".
    const ctx = lookup?.forDelivery(d.deliverable_id, null);
    items.push({
      kind: "delivery",
      id: `delivery-${d.id}`,
      itemId: d.id,
      status: d.status,
      title: ctx?.title ?? null,
      productSlug: ctx?.productSlug,
      detailHref: ctx?.detailHref ?? null,
      resolvedAt: d.decided_at ?? d.created_at,
    });
  }
  for (const c of checkpoints) {
    items.push({
      kind: "decision",
      id: `checkpoint-${c.id}`,
      checkpointId: c.id,
      question: c.question,
      resolution: c.resolution,
      resolvedAt: c.resolved_at ?? "",
    });
  }
  for (const e of log) {
    items.push({
      kind: "knowledge",
      id: `decision-${e.id}`,
      decisionKind: e.decision_kind,
      resolvedAt: e.created_at,
    });
  }
  // Newest-first across kinds. Items with an unparseable timestamp sort last.
  return items.sort((a, b) => Date.parse(b.resolvedAt) - Date.parse(a.resolvedAt));
}

/** Read all three resolved queues in parallel and return the merged list. A
 *  single optional queue failing degrades to empty rather than blanking the
 *  surface. */
export async function listResolvedDecisions(): Promise<ResolvedDecision[]> {
  const [deliveries, checkpoints, log, runs, deliverables, products] = await Promise.all([
    listResolvedSafeMode().catch(emptyOnApiError<SafeModeResolvedItem>),
    listResolvedCheckpoints().catch(emptyOnApiError<ResolvedCheckpointItem>),
    listDecisionsLog().catch(emptyOnApiError<DecisionLogEntry>),
    // The review-context join — same three reads the Pending list already does.
    // Each degrades to empty so a blip just falls back to the bare outcome.
    listRuns(_RUN_WINDOW).catch(emptyOnApiError<Run>),
    listDeliverables(_RUN_WINDOW).catch(emptyOnApiError<Deliverable>),
    listProducts().catch(emptyOnApiError<Product>),
  ]);
  const lookup = buildReviewLookup(runs, deliverables, products);
  return toResolvedDecisions(deliveries, checkpoints, log, lookup);
}
