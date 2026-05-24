/**
 * Composes the Brief (Glance) view-model from REAL backend endpoints.
 *
 * REAL today:
 *  - lanes        ← /api/v1/products  +  per-product latest /api/v1/runs status
 *  - needsYou     ← /api/v1/decisions (pending proposals) + /api/v1/safemode/queue
 *  - recentlyShipped ← /api/v1/deliverables (real Deliverable rows, newest first)
 *
 * All three surfaces are now sourced from live endpoints, so a successful read
 * never carries demo/placeholder data — `BriefView.placeholder` is false on a
 * real read (even an empty workspace, which renders calm empty states). It
 * flips true ONLY when a hard failure forces the demo-lane fallback below, so
 * the surface shows a calm board instead of an error wall.
 *
 * Remaining DERIVED detail (no schema gap forced into the backend): a
 * Deliverable carries `run_id` but no `product_id`, so a shipped item's
 * product attribution is resolved by cross-referencing the runs list
 * (run_id → product_id → slug); it degrades to "workspace" when the producing
 * run is older than the runs window.
 */

import { ApiError } from "./client";
import { listPendingProposals } from "./decisions";
import { listDeliverables } from "./deliverables";
import { PLACEHOLDER_LANES } from "./placeholder";
import { listProducts } from "./products";
import { listRuns } from "./runs";
import { listSafeModeQueue } from "./safemode";
import type {
  ArtifactType,
  BriefView,
  Deliverable,
  DeliverableType,
  LaneState,
  NeedsYouItem,
  Product,
  ProductLane,
  Proposal,
  Run,
  RunStatus,
  SafeModeItem,
  ShippedItem,
} from "./types";

/** Map a run's lifecycle status → the calm lane-state vocabulary (UX §3.3). */
function laneStateForRun(status: RunStatus): LaneState {
  switch (status) {
    case "open":
      // freshly created, not yet picked up → "just triggered · decomposing…"
      return "triggered";
    case "running":
      return "working";
    case "review_ready":
      // verified output waiting on the founder → the amber needs-you state
      return "needs-you";
    case "shipped":
      return "shipped";
    default:
      // failed / cancelled → calm idle rather than a red error lane
      return "idle";
  }
}

/** Plain-language status line for a real lane derived from its latest run. */
function laneStatusFor(state: LaneState, hasRun: boolean): string {
  if (!hasRun) return "—";
  switch (state) {
    case "triggered":
      return "just started · decomposing…";
    case "working":
      return "working on your latest direction";
    case "needs-you":
      return "ready for your review";
    case "shipped":
      return "shipped · verified";
    default:
      return "—";
  }
}

/** Newest run per product_id from a newest-first run list. */
function latestRunByProduct(runs: Run[]): Map<string, Run> {
  const latest = new Map<string, Run>();
  for (const run of runs) {
    if (run.product_id && !latest.has(run.product_id)) {
      latest.set(run.product_id, run);
    }
  }
  return latest;
}

function lanesFromProducts(products: Product[], runs: Run[]): ProductLane[] {
  const byProduct = latestRunByProduct(runs);
  return products.map((p) => {
    const run = byProduct.get(p.id);
    const state: LaneState = run ? laneStateForRun(run.status) : "idle";
    return {
      id: p.id,
      slug: p.slug,
      name: p.name,
      state,
      status: laneStatusFor(state, run !== undefined),
    };
  });
}

/** Resolve a product's slug from its id (for needs-you / shipped attribution). */
function productSlug(products: Product[], productId: string | null): string {
  if (!productId) return "workspace";
  return products.find((p) => p.id === productId)?.slug ?? "workspace";
}

function needsYouFrom(proposals: Proposal[], queue: SafeModeItem[]): NeedsYouItem[] {
  const items: NeedsYouItem[] = [];
  for (const p of proposals) {
    items.push({
      id: `proposal-${p.id}`,
      productSlug: "knowledge",
      question: `Approve ${p.action_kind} on “${p.action_path}”?`,
    });
  }
  for (const item of queue) {
    items.push({
      id: `safemode-${item.id}`,
      productSlug: "delivery",
      question: "A delivery is held in Safe Mode — approve to send it out?",
      // Resolvable in-place: approve dispatches it out, deny dismisses it.
      resolve: { kind: "safemode", itemId: item.id },
    });
  }
  return items;
}

/** Map a backend DeliverableType → the calmer ArtifactType UI vocabulary
 *  (UX §4 — deliverables render with a per-type marker). */
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
      // code (and any future bare artifact) → the generic file marker.
      return "file";
  }
}

/** Plain-language "where it landed" label for a deliverable type. */
function sourceFor(type: DeliverableType): string {
  switch (type) {
    case "pr":
      return "opened a pull request";
    case "code":
      return "committed to the repo";
    case "page":
      return "published a page";
    case "page_image":
      return "rendered a page preview";
    default:
      return "shipped";
  }
}

/** First non-empty line of a summary as the item title; calm fallback if none. */
function titleFor(summary: string | null): string {
  const first = (summary ?? "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  return first ?? "Shipped deliverable";
}

/** Resolve a deliverable's product slug via its producing run (Deliverable has
 *  no product_id of its own). Degrades to "workspace" when the run is outside
 *  the runs window. */
function productSlugForRun(runs: Run[], products: Product[], runId: string): string {
  const run = runs.find((r) => r.id === runId);
  return productSlug(products, run?.product_id ?? null);
}

/** Real Deliverable rows → recently-shipped items. Deliverables only exist for
 *  verified runs, so the verdict is the calm "This is verified". */
function recentlyShippedFrom(
  deliverables: Deliverable[],
  runs: Run[],
  products: Product[],
): ShippedItem[] {
  return deliverables.map((d) => {
    const item: ShippedItem = {
      id: d.id,
      title: titleFor(d.summary),
      productSlug: productSlugForRun(runs, products, d.run_id),
      source: sourceFor(d.deliverable_type),
      artifactType: artifactTypeFor(d.deliverable_type),
      verdict: "This is verified",
    };
    if (d.artifact_uri) item.link = d.artifact_uri;
    return item;
  });
}

export async function getBrief(): Promise<BriefView> {
  try {
    // Fetch the real surfaces in parallel. A 4xx on a CORE surface (products /
    // runs) bubbles up to the fallback below rather than half-rendering. The
    // optional surfaces (decisions / safemode / deliverables) degrade to empty
    // on their own ApiError so one of them failing never blanks the Brief.
    const [products, runs, proposals, queue, deliverables] = await Promise.all([
      listProducts(),
      listRuns(),
      listPendingProposals().catch(emptyOnApiError<Proposal>),
      listSafeModeQueue().catch(emptyOnApiError<SafeModeItem>),
      listDeliverables(6).catch(emptyOnApiError<Deliverable>),
    ]);

    return {
      needsYou: needsYouFrom(proposals, queue),
      lanes: lanesFromProducts(products, runs),
      recentlyShipped: recentlyShippedFrom(deliverables, runs, products),
      // All three surfaces are real now. A successful read — even an empty
      // workspace — is never placeholder; only the hard-failure demo fallback
      // below sets it true.
      placeholder: false,
    };
  } catch (error) {
    // A 401 is auth-expired, NOT a transient network blip: it must propagate so
    // the global 401 handler (apiFetch) + the gate redirect to /login fire,
    // instead of masking the expired session behind the calm demo board.
    if (error instanceof ApiError && error.status === 401) throw error;
    // No backend / non-401 4xx-5xx mid-load → show the demo lanes rather than an
    // error wall, so the surface stays calm during a genuine network failure.
    if (!(error instanceof ApiError) && !(error instanceof TypeError)) throw error;
    return {
      needsYou: [],
      lanes: PLACEHOLDER_LANES,
      recentlyShipped: [],
      placeholder: true,
    };
  }
}

/** Swallow a per-surface ApiError into an empty list so a single optional
 *  surface (decisions / safemode) failing does not blank the whole Brief. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}
