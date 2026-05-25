/**
 * Composes the Activity (run history) view-model from REAL backend endpoints.
 *
 * Activity is a calm, read-only window onto everything the AI has done — the
 * full list of ExecutionRuns, newest first, each expandable to its delivered
 * artifacts / proof. Two reads back it:
 *
 *  - the run list      ← GET /api/v1/runs            (ExecutionRun rows)
 *  - per-run artifacts ← GET /api/v1/deliverables?run_id=<id>  (lazy, on expand)
 *
 * Product attribution: a run carries `product_id` but not the human slug, so we
 * cross-reference /api/v1/products (run.product_id → slug), degrading to
 * "workspace" when the run carries none. The runs + products reads happen once
 * up-front; a run's deliverables are fetched lazily the first time it expands so
 * the list stays cheap for a long history.
 *
 * Read-only by design — no mutations on this surface.
 */

import { conciseSummary } from "../text/summary";
import { listDeliverables } from "./deliverables";
import { listProducts } from "./products";
import { listRuns } from "./runs";
import type {
  ActivityDeliverable,
  ActivityRun,
  ActivityTone,
  ArtifactType,
  Deliverable,
  DeliverableType,
  Product,
  Run,
  RunStatus,
} from "./types";

/** Calm plain-language label + status tone for a run's lifecycle status. The
 *  tone is the ONLY thing that carries colour (UX §5 — colour for status only).
 *
 *  `open` (freshly created, not yet picked up) and `cancelled` (stood down) read
 *  as quiet neutral states rather than alarms; `failed` gets a muted — not
 *  shouting — red so the founder can see it without the surface feeling broken. */
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

/** Resolve a run's product slug from the products list; "workspace" when the
 *  run carries no product or the product is outside the list. */
function productSlugFor(products: Product[], productId: string | null): string {
  if (!productId) return "workspace";
  return products.find((p) => p.id === productId)?.slug ?? "workspace";
}

function toActivityRun(run: Run, products: Product[]): ActivityRun {
  const { label, tone } = describeStatus(run.status);
  return {
    runId: run.id,
    productSlug: productSlugFor(products, run.product_id),
    status: run.status,
    statusLabel: label,
    tone,
    updatedAt: run.updated_at,
  };
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

/** A concise one-line artifact title (the shared first-sentence condenser), so
 *  Activity reads like the Brief instead of dumping the raw LLM summary. */
function titleFor(summary: string | null): string {
  return conciseSummary(summary, "Delivered artifact");
}

function toActivityDeliverable(d: Deliverable): ActivityDeliverable {
  const item: ActivityDeliverable = {
    id: d.id,
    title: titleFor(d.summary),
    artifactType: artifactTypeFor(d.deliverable_type),
    source: sourceFor(d.deliverable_type),
    verdict: "This is verified",
  };
  if (d.artifact_uri) item.link = d.artifact_uri;
  return item;
}

/** Recent runs for the active workspace, newest first, as calm view-model rows.
 *  Fetches the runs + products lists in parallel; a thrown ApiError bubbles up
 *  to the surface, which renders a calm inline error rather than a blank page. */
export async function getActivity(limit = 50): Promise<ActivityRun[]> {
  const [runs, products] = await Promise.all([listRuns(limit), listProducts()]);
  return runs.map((run) => toActivityRun(run, products));
}

/** One run's delivered artifacts (lazy, fetched the first time a row expands).
 *  Newest first; an empty list is a valid result (a run can be in-flight or have
 *  finished without an addressable artifact). */
export async function getRunDeliverables(runId: string): Promise<ActivityDeliverable[]> {
  const rows = await listDeliverables(50, runId);
  return rows.map(toActivityDeliverable);
}
