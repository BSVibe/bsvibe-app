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
 *  - shipped art.  ← GET /api/v1/deliverables?run_id=  (per shipped run)
 *
 * Header status is derived from the product's latest run (the runs list is
 * newest-first; the first run carrying this product_id is its latest). The
 * "Shipped" section eagerly fetches the deliverables of the product's shipped
 * runs only (a focused view wants its proof visible, and shipped runs are a
 * small slice of the history), in parallel.
 *
 * An unknown slug is a first-class result: `getProductDetail` returns `null`
 * (NOT a thrown error), so the surface can render a calm "I don't know that
 * product" instead of an error wall. A real backend failure (4xx/5xx, no
 * network) bubbles up as a thrown ApiError/TypeError for the surface's inline
 * error state.
 *
 * Read-only by design — no mutations on this surface.
 */

import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
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

/** Calm plain-language label + status tone for a run's lifecycle status — the
 *  same vocabulary the Activity surface uses (UX §5, colour for status only). */
function describeStatus(status: RunStatus): { label: string; tone: ActivityTone } {
  switch (status) {
    case "open":
      return { label: "Just started", tone: "neutral" };
    case "running":
      return { label: "Working", tone: "working" };
    case "review_ready":
      return { label: "Needs your review", tone: "review" };
    case "shipped":
      return { label: "Shipped", tone: "shipped" };
    case "failed":
      return { label: "Didn’t finish", tone: "failed" };
    default:
      // cancelled — stood down on purpose, not an error.
      return { label: "Stood down", tone: "neutral" };
  }
}

/** Plain-language headline for the product header, derived from its latest run.
 *  Calm and reassuring — never machinery (no rounds / cost). */
function headlineFor(latest: Run | undefined): { status: string; tone: ActivityTone } {
  if (!latest) {
    return { status: "Nothing running yet — give it a Direction.", tone: "neutral" };
  }
  const { tone } = describeStatus(latest.status);
  switch (latest.status) {
    case "open":
      return { status: "Just started · decomposing your latest direction…", tone };
    case "running":
      return { status: "Working on your latest direction.", tone };
    case "review_ready":
      return { status: "Ready for your review.", tone };
    case "shipped":
      return { status: "All caught up — latest work shipped & verified.", tone };
    case "failed":
      return { status: "The latest run didn’t finish.", tone };
    default:
      return { status: "The latest run was stood down.", tone };
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

/** First non-empty line of a summary as the item title; calm fallback if none. */
function titleFor(summary: string | null): string {
  const first = (summary ?? "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  return first ?? "Shipped deliverable";
}

function toShippedItem(d: Deliverable, productSlug: string): ShippedItem {
  const item: ShippedItem = {
    id: d.id,
    title: titleFor(d.summary),
    productSlug,
    source: sourceFor(d.deliverable_type),
    artifactType: artifactTypeFor(d.deliverable_type),
    verdict: "This is verified",
  };
  if (d.artifact_uri) item.link = d.artifact_uri;
  return item;
}

function toDetailRun(run: Run): ProductDetailRun {
  const { label, tone } = describeStatus(run.status);
  return {
    runId: run.id,
    status: run.status,
    statusLabel: label,
    tone,
    updatedAt: run.updated_at,
    shipped: run.status === "shipped",
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
  const detailRuns = productRuns.map(toDetailRun);
  const latest = productRuns[0];
  const { status: currentStatus, tone: currentTone } = headlineFor(latest);

  // Eagerly fetch the deliverables of the product's shipped runs, in parallel —
  // a focused view wants its proof visible without a per-run expand. Shipped
  // runs are a small slice of the history, so this stays cheap. A per-run
  // deliverables read failing degrades that run to no artifacts rather than
  // blanking the whole view.
  const shippedRuns = productRuns.filter((r) => r.status === "shipped");
  const perRun = await Promise.all(
    shippedRuns.map((r) => listDeliverables(50, r.id).catch((): Deliverable[] => [])),
  );
  const shipped = perRun.flat().map((d) => toShippedItem(d, product.slug));

  return {
    id: product.id,
    slug: product.slug,
    name: product.name,
    repoUrl: product.repo_url,
    currentStatus,
    currentTone,
    runs: detailRuns,
    shipped,
  };
}
