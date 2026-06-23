/**
 * Composes the merged Brief / Work-Home view-model from REAL backend endpoints.
 *
 * This is the single "what is BSVibe doing + what has it done" surface (the old
 * Brief and Activity tabs were merged because they overlapped). It folds:
 *  - working   ← /api/v1/runs in an in-flight status (open / running)
 *  - needsYou  ← /api/v1/decisions (pending proposals) + /api/v1/safemode/queue
 *  - stream    ← /api/v1/runs (ALL, newest first) joined to /api/v1/deliverables
 *               by run_id (the chronological work history)
 *
 * Every surface is live, so a successful read — even an empty workspace — is
 * never `placeholder`; that flips true ONLY on a hard non-401 failure, so the
 * surface shows calm empty states instead of an error wall. A 401 propagates so
 * the global auth handler can redirect to /login.
 *
 * Status LABELS are intentionally NOT composed here — the components translate
 * `status` via i18n (the data layer stays locale-free). Titles are user/LLM
 * content (a shipped deliverable's concise summary, or the run's Direction).
 */

import { isActiveStatus } from "../runs/status";
import { conciseSummary } from "../text/summary";
import { ApiError } from "./client";
import { listPendingProposals } from "./decisions";
import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
import { type ReviewLookup, buildReviewLookup } from "./review-context";
import { listRuns } from "./runs";
import { listSafeModeQueue } from "./safemode";
import type {
  ActiveWork,
  ArtifactType,
  BriefView,
  Deliverable,
  DeliverableType,
  NeedsYouItem,
  Product,
  Proposal,
  Run,
  SafeModeItem,
  WorkStreamItem,
} from "./types";

const _RUN_WINDOW = 50;

/** Resolve a product's slug from its id; "workspace" when none / not found. */
function productSlug(products: Product[], productId: string | null): string {
  if (!productId) return "workspace";
  return products.find((p) => p.id === productId)?.slug ?? "workspace";
}

function needsYouFrom(
  proposals: Proposal[],
  queue: SafeModeItem[],
  lookup: ReviewLookup,
): NeedsYouItem[] {
  const items: NeedsYouItem[] = [];
  for (const p of proposals) {
    items.push({
      id: `proposal-${p.id}`,
      productSlug: "knowledge",
      question: `Approve ${p.action_kind} on “${p.action_path}”?`,
    });
  }
  for (const item of queue) {
    // Join the run/deliverable so "Needs you" names WHAT is held + links to the
    // proof, instead of a bare "a delivery is held in Safe Mode".
    const ctx = lookup.forDelivery(item.deliverable_id, item.run_id ?? null);
    items.push({
      id: `safemode-${item.id}`,
      productSlug: ctx.productSlug !== "workspace" ? ctx.productSlug : "delivery",
      question: "A delivery is held in Safe Mode. Approve to send it out?",
      title: ctx.title,
      detailHref: ctx.detailHref,
      // Resolvable in-place: approve dispatches it out, deny dismisses it.
      resolve: { kind: "safemode", itemId: item.id },
    });
  }
  return items;
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
      title: r.intent,
      productSlug: productSlug(products, r.product_id),
      status: r.status,
      startedAt: r.created_at,
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
        title: summaryTitle || run.intent || null,
        productSlug: productSlug(products, run.product_id),
        status: run.status,
        updatedAt: run.updated_at,
        deliverableId: deliverable?.id ?? null,
        artifactType: deliverable ? artifactTypeFor(deliverable.deliverable_type) : null,
      };
    });
}

export async function getBrief(): Promise<BriefView> {
  try {
    // Core surfaces (products / runs) bubble a 4xx to the fallback; the optional
    // surfaces (decisions / safemode / deliverables) degrade to empty on their
    // own ApiError so one failing never blanks the whole surface.
    const [products, runs, proposals, queue, deliverables] = await Promise.all([
      listProducts(),
      listRuns(_RUN_WINDOW),
      listPendingProposals().catch(emptyOnApiError<Proposal>),
      listSafeModeQueue().catch(emptyOnApiError<SafeModeItem>),
      listDeliverables(_RUN_WINDOW).catch(emptyOnApiError<Deliverable>),
    ]);

    const lookup = buildReviewLookup(runs, deliverables, products);
    return {
      working: activeWorkFrom(runs, products),
      needsYou: needsYouFrom(proposals, queue, lookup),
      stream: workStreamFrom(runs, deliverables, products),
      placeholder: false,
    };
  } catch (error) {
    // A 401 is auth-expired — propagate so the global handler + gate redirect to
    // /login fire, rather than masking it behind calm empty states.
    if (error instanceof ApiError && error.status === 401) throw error;
    if (!(error instanceof ApiError) && !(error instanceof TypeError)) throw error;
    return { working: [], needsYou: [], stream: [], placeholder: true };
  }
}

/** Swallow a per-surface ApiError into an empty list so a single optional
 *  surface (decisions / safemode / deliverables) failing does not blank it. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}
