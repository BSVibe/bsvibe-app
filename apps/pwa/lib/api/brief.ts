/**
 * Composes the unified Brief / Work-Home view-model from REAL backend endpoints.
 *
 * This is the single "what needs me + what is BSVibe doing + what has it done"
 * surface. It folds:
 *  - needsYou  ← the pending Safe-Mode held deliveries (/api/v1/safemode/queue)
 *               + paused-run checkpoints (/api/v1/checkpoints), joined to their
 *               run/deliverable for a concise title + proof link. Resolved
 *               inline in the Brief (DeliveryRow / CheckpointRow).
 *  - working   ← /api/v1/runs in an in-flight status (open / running)
 *  - stream    ← /api/v1/runs (ALL, newest first) joined to /api/v1/deliverables
 *               by run_id (the chronological work history)
 *
 * R4 — decisions are UNIFIED back into the Brief: a decision is an inline STATE
 * of a work-stream, resolved HERE with context. This REVERSES L7 (#6), which had
 * removed the needs-you block to avoid duplicating a separate Decisions tab —
 * decisions now LIVE in the Brief. The needs-you list reuses the SAME pending
 * aggregation the Decisions tab uses (listPendingDecisions → review-context
 * join), filtered to the two INLINE-resolvable kinds (delivery + checkpoint);
 * knowledge proposals (which open a focused detail panel) stay on the Decisions
 * tab.
 *
 * Every surface is live, so a successful read — even an empty workspace — is
 * never `placeholder`; that flips true ONLY on a hard non-401 failure, so the
 * surface shows calm empty states instead of an error wall. A 401 propagates so
 * the global auth handler can redirect to /login. needsYou degrades to empty on
 * its own blip (listPendingDecisions already swallows per-queue failures) so it
 * never blanks the rest of the Brief.
 *
 * Status LABELS are intentionally NOT composed here — the components translate
 * `status` via i18n (the data layer stays locale-free). Titles are user/LLM
 * content (a shipped deliverable's concise summary, or the run's Direction).
 */

import { isActiveStatus } from "../runs/status";
import { conciseSummary } from "../text/summary";
import { ApiError } from "./client";
import { listDeliverables } from "./deliverables";
import { listPendingDecisions } from "./pending";
import { listProducts } from "./products";
import { listRuns } from "./runs";
import type {
  ActiveWork,
  ArtifactType,
  BriefView,
  Deliverable,
  DeliverableType,
  PendingDecision,
  Product,
  Run,
  WorkStreamItem,
} from "./types";

const _RUN_WINDOW = 50;

/** Resolve a product's slug from its id; "workspace" when none / not found. */
function productSlug(products: Product[], productId: string | null): string {
  if (!productId) return "workspace";
  return products.find((p) => p.id === productId)?.slug ?? "workspace";
}

/** Map a backend DeliverableType → the calmer ArtifactType UI vocabulary. */
function artifactTypeFor(type: DeliverableType): ArtifactType {
  switch (type) {
    case "pr":
      return "pr";
    case "page_image":
      return "image";
    case "page":
    case "direct_output":
      return "doc";
    default:
      return "file";
  }
}

/** The actively-running work (in-flight runs) for the "Working on now" hero,
 *  newest first. */
function activeWorkFrom(runs: Run[], products: Product[]): ActiveWork[] {
  return runs
    .filter((r) => isActiveStatus(r.status))
    .map((r) => ({
      runId: r.id,
      // L8 — short plain-language task title; fall back to framed_intent / raw.
      title: r.summary_title || r.framed_intent || r.intent,
      productSlug: productSlug(products, r.product_id),
      status: r.status,
      // L9 — count elapsed from the last restart (retry) when present, so a
      // retried run's clock resets instead of counting from the first start.
      startedAt: r.restarted_at || r.created_at,
    }));
}

/** The full chronological work stream (every run, newest first), each joined to
 *  the deliverable it produced (when any) for the title + "View report" link. */
function workStreamFrom(
  runs: Run[],
  deliverables: Deliverable[],
  products: Product[],
): WorkStreamItem[] {
  // run_id → its latest deliverable (the list is newest-first, so the first
  // deliverable seen for a run_id is the most recent).
  const byRun = new Map<string, Deliverable>();
  for (const d of deliverables) {
    if (!byRun.has(d.run_id)) byRun.set(d.run_id, d);
  }
  // Active (open / running) runs live in the "Working on now" hero — the stream
  // is the DONE history, so they're excluded here to avoid duplication.
  return runs
    .filter((r) => !isActiveStatus(r.status))
    .map((run) => {
      const deliverable = byRun.get(run.id);
      const summaryTitle = deliverable?.summary ? conciseSummary(deliverable.summary, "") : "";
      return {
        runId: run.id,
        // L8 — short plain-language task title preferred over the deliverable's
        // file-list summary and the raw, developer-y Direction.
        title: run.summary_title || run.framed_intent || summaryTitle || run.intent || null,
        productSlug: productSlug(products, run.product_id),
        status: run.status,
        updatedAt: run.updated_at,
        deliverableId: deliverable?.id ?? null,
        artifactType: deliverable ? artifactTypeFor(deliverable.deliverable_type) : null,
      };
    });
}

/** All three INLINE-resolvable needs-you kinds the Brief surfaces (R9): held
 *  deliveries, paused-run checkpoints, AND canon/knowledge proposals — every
 *  pending decision is judged in the Brief now (the Decisions tab is gone), so
 *  this no longer filters anything out. Kept as a named seam for clarity. */
function inlineNeedsYou(items: PendingDecision[]): PendingDecision[] {
  return items.filter(
    (i) => i.kind === "delivery" || i.kind === "decision" || i.kind === "knowledge",
  );
}

export async function getBrief(): Promise<BriefView> {
  try {
    // Core surfaces (products / runs) bubble a 4xx to the fallback; the optional
    // deliverables surface degrades to empty on its own ApiError so it failing
    // never blanks the whole surface. The needsYou aggregation already swallows
    // each pending queue's per-surface failure, so it degrades to [] on a blip.
    const [products, runs, deliverables, pending] = await Promise.all([
      listProducts(),
      listRuns(_RUN_WINDOW),
      listDeliverables(_RUN_WINDOW).catch(emptyOnApiError<Deliverable>),
      listPendingDecisions().catch(emptyOnApiError<PendingDecision>),
    ]);

    return {
      needsYou: inlineNeedsYou(pending),
      working: activeWorkFrom(runs, products),
      stream: workStreamFrom(runs, deliverables, products),
      placeholder: false,
    };
  } catch (error) {
    // A 401 is auth-expired — propagate so the global handler + gate redirect to
    // /login fire, rather than masking it behind calm empty states.
    if (error instanceof ApiError && error.status === 401) throw error;
    if (!(error instanceof ApiError) && !(error instanceof TypeError)) throw error;
    return { needsYou: [], working: [], stream: [], placeholder: true };
  }
}

/** Swallow a per-surface ApiError into an empty list so a single optional
 *  surface (deliverables) failing does not blank it. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}
