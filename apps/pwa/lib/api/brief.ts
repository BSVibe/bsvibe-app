/**
 * Composes the Brief (Glance) view-model from REAL backend endpoints.
 *
 * REAL today:
 *  - lanes        ← /api/v1/products  +  per-product latest /api/v1/runs status
 *  - needsYou     ← /api/v1/decisions (pending proposals) + /api/v1/safemode/queue
 *  - recentlyShipped ← /api/v1/runs filtered to shipped / review_ready
 *
 * STILL PLACEHOLDER (no endpoint yet — see placeholder.ts):
 *  - the shipped item *title* and *source/artifact-type* detail. There is no
 *    deliverable-read endpoint (only runs), so a shipped run renders with a
 *    derived plain-language title + a generic "shipped" source. While any
 *    shipped item carries that derived detail, `BriefView.placeholder` is true.
 *
 * An empty / fresh workspace is a real read → calm empty states (NOT demo
 * data). The demo lanes are used ONLY as a fallback when the network/auth
 * fails mid-load, so the surface never shows an error wall.
 */

import { ApiError } from "./client";
import { listPendingProposals } from "./decisions";
import { PLACEHOLDER_LANES } from "./placeholder";
import { listProducts } from "./products";
import { listRuns } from "./runs";
import { listSafeModeQueue } from "./safemode";
import type {
  BriefView,
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
    });
  }
  return items;
}

/** Shipped runs → recently-shipped items. The title/source is DERIVED (no
 *  deliverable-read endpoint), so this carries placeholder detail. */
function recentlyShippedFrom(runs: Run[], products: Product[]): ShippedItem[] {
  return runs
    .filter((r) => r.status === "shipped" || r.status === "review_ready")
    .slice(0, 6)
    .map((r) => {
      const slug = productSlug(products, r.product_id);
      const ready = r.status === "review_ready";
      return {
        id: r.id,
        title: ready ? "Output ready for review" : "Shipped deliverable",
        productSlug: slug,
        source: ready ? "awaiting review" : "shipped",
        artifactType: "file" as const,
        verdict: ready ? "Ready for your review" : "This is verified",
      };
    });
}

export async function getBrief(): Promise<BriefView> {
  try {
    // Fetch the real surfaces in parallel. A 4xx on any one (e.g. an endpoint
    // not yet reachable) bubbles up to the fallback below rather than half-
    // rendering the surface.
    const [products, runs, proposals, queue] = await Promise.all([
      listProducts(),
      listRuns(),
      listPendingProposals().catch(emptyOnApiError<Proposal>),
      listSafeModeQueue().catch(emptyOnApiError<SafeModeItem>),
    ]);

    const recentlyShipped = recentlyShippedFrom(runs, products);
    return {
      needsYou: needsYouFrom(proposals, queue),
      lanes: lanesFromProducts(products, runs),
      recentlyShipped,
      // Lanes + needs-you are fully real. Only the shipped-item title/source is
      // derived (no deliverable endpoint), so placeholder is true iff we showed
      // any shipped item with that derived detail.
      placeholder: recentlyShipped.length > 0,
    };
  } catch (error) {
    // No backend / not authed mid-load → show the demo lanes rather than an
    // error wall. A real 4xx still surfaces the demo (the gate handles 401).
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
