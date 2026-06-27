/**
 * Composes the Product detail view-model from REAL backend endpoints — the
 * focused per-product window behind `/products/[slug]`.
 *
 * There is NO `GET /api/v1/products/{slug}` or run-by-product filter on the
 * backend today, so the whole view is composed CLIENT-SIDE from the existing
 * list endpoints:
 *
 *  - the product   ← GET /api/v1/products              (found by slug)
 *  - its runs      ← GET /api/v1/runs                  (filtered by product_id)
 *  - deliverables  ← GET /api/v1/deliverables?run_id=  (per shipped/review run)
 *
 * Header status is derived from the product's latest run (the runs list is
 * newest-first; the first run carrying this product_id is its latest). We
 * eagerly fetch deliverables for runs that HAVE one — shipped runs (the
 * "Shipped" proof section) AND review-ready runs (so a "Needs your review" row
 * links straight to its report, no run-detail detour) — in parallel. Both are a
 * small slice of the history, so this stays cheap.
 *
 * An unknown slug is a first-class result: `getProductDetail` returns `null`
 * (NOT a thrown error), so the surface can render a calm "I don't know that
 * product" instead of an error wall. A real backend failure (4xx/5xx, no
 * network) bubbles up as a thrown ApiError/TypeError for the surface's inline
 * error state.
 *
 * Read-only by design — no mutations on this surface.
 */

import { conciseSummary } from "../text/summary";
import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
import { type ReviewLookup, buildReviewLookup } from "./review-context";
import { listRuns } from "./runs";
import type {
  ActivityTone,
  ArtifactType,
  Deliverable,
  DeliverableType,
  Product,
  ProductDetailRun,
  ProductDetailView,
  Run,
  RunStatus,
  ShippedItem,
} from "./types";

/** Status tone for a run's lifecycle status — the lone colour signal (UX §5).
 *  The plain-language LABEL is no longer derived here: the runs surface
 *  translates it from the shared `STATUS_LABEL_KEY` so it's localized, not the
 *  hardcoded English this view-model used to carry. */
function toneFor(status: RunStatus): ActivityTone {
  switch (status) {
    case "running":
      return "working";
    case "review_ready":
      return "review";
    case "shipped":
      return "shipped";
    case "failed":
      return "failed";
    default:
      // open (just started) + cancelled (stood down on purpose) — calm/neutral.
      return "neutral";
  }
}

/** i18n KEY (under the `products` namespace) for the product-header headline,
 *  derived from its latest run — translated in ProductHeader, not hardcoded here.
 *  Calm and reassuring — never machinery (no rounds / cost). */
function headlineFor(latest: Run | undefined): { statusKey: string; tone: ActivityTone } {
  if (!latest) {
    return { statusKey: "headlineEmpty", tone: "neutral" };
  }
  const tone = toneFor(latest.status);
  switch (latest.status) {
    case "open":
      return { statusKey: "headlineJustStarted", tone };
    case "running":
      return { statusKey: "headlineWorking", tone };
    case "review_ready":
      return { statusKey: "headlineReview", tone };
    case "shipped":
      return { statusKey: "headlineShipped", tone };
    case "failed":
      return { statusKey: "headlineFailed", tone };
    default:
      return { statusKey: "headlineStood", tone };
  }
}

/** Map a backend DeliverableType → the calmer ArtifactType UI vocabulary
 *  (UX §4 — deliverables render with a per-type marker). Mirrors brief.ts /
 *  activity.ts so the three surfaces feel like one product. */
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

/** A concise one-line summary for a shipped deliverable (the shared first-
 *  sentence condenser), so the product surfaces read like the Brief. Empty when
 *  the deliverable has no summary — the component supplies a TRANSLATED fallback
 *  (`products.untitled`), since this lib can't reach i18n. */
function titleFor(summary: string | null): string {
  return conciseSummary(summary, "");
}

function toShippedItem(d: Deliverable, productSlug: string): ShippedItem {
  const item: ShippedItem = {
    id: d.id,
    title: titleFor(d.summary),
    productSlug,
    source: sourceFor(d.deliverable_type),
    artifactType: artifactTypeFor(d.deliverable_type),
    // B4 trust-integrity: the verdict derives from the backend-authoritative
    // `verified` flag (a PASSED VerificationResult), NOT from the deliverable
    // existing. A hollow deliverable reads honestly as awaiting verification —
    // the founder never sees a green "This is verified" without real proof.
    verdict: d.verified ? "This is verified" : "Awaiting verification",
  };
  if (d.artifact_uri) item.link = d.artifact_uri;
  return item;
}

function toDetailRun(run: Run, lookup: ReviewLookup): ProductDetailRun {
  const ctx = lookup.forRun(run.id);
  return {
    runId: run.id,
    status: run.status,
    tone: toneFor(run.status),
    updatedAt: run.updated_at,
    shipped: run.status === "shipped",
    title: ctx.title,
    detailHref: ctx.detailHref,
  };
}

/**
 * Resolve the focused detail view for ONE product, identified by its slug.
 *
 * Returns `null` when no product in the active workspace carries that slug — an
 * expected, calm "unknown product" outcome, NOT an error. A backend failure
 * (the products / runs read throwing) bubbles up to the surface's inline error
 * state instead.
 *
 * @param runLimit how many runs to scan for this product's history (the runs
 *   list is workspace-wide and newest-first; we client-side filter to this
 *   product_id). Defaults to a generous window so the header status reflects
 *   the genuine latest run.
 */
export async function getProductDetail(
  slug: string,
  runLimit = 100,
): Promise<ProductDetailView | null> {
  const [products, runs] = await Promise.all([listProducts(), listRuns(runLimit)]);

  const product = products.find((p) => p.slug === slug) as Product | undefined;
  if (!product) return null;

  // Runs for THIS product, preserving the newest-first order of the list.
  const productRuns = runs.filter((r) => r.product_id === product.id);
  const latest = productRuns[0];
  const { statusKey: currentStatusKey, tone: currentTone } = headlineFor(latest);

  // Eagerly fetch the deliverables of runs that HAVE one — shipped runs (for the
  // "Shipped" proof section) AND review-ready runs (so a "Needs your review" row
  // links straight to its report, with no run-detail status detour). Both are a
  // small slice of the history, so this stays cheap; a per-run deliverables read
  // failing degrades that run to no artifacts rather than blanking the view.
  const runsWithDeliverable = productRuns.filter(
    (r) => r.status === "shipped" || r.status === "review_ready",
  );
  const perRun = await Promise.all(
    runsWithDeliverable.map((r) => listDeliverables(50, r.id).catch((): Deliverable[] => [])),
  );
  const deliverables = perRun.flat();

  // "Shipped" lists ONLY shipped runs' deliverables — review-ready work isn't
  // shipped, even though we fetched its deliverable to resolve the report link.
  const shippedRunIds = new Set(productRuns.filter((r) => r.status === "shipped").map((r) => r.id));
  const shipped = deliverables
    .filter((d) => shippedRunIds.has(d.run_id))
    .map((d) => toShippedItem(d, product.slug));

  // Title + report link per run: any run with a deliverable links straight to
  // its report (/deliverables/<id>); the rest to the run — so every row opens.
  const lookup = buildReviewLookup(productRuns, deliverables, products);
  const detailRuns = productRuns.map((r) => toDetailRun(r, lookup));

  return {
    id: product.id,
    slug: product.slug,
    name: product.name,
    repoUrl: product.repo_url,
    currentStatusKey,
    currentTone,
    runs: detailRuns,
    shipped,
  };
}
