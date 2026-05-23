/**
 * Wire + view-model types for the PWA.
 *
 * The `Supabase*`, `Workspace`, and `Product` shapes mirror the backend
 * response models 1:1 (backend/api/auth/routes.py, backend/api/v1/
 * workspaces.py, backend/api/v1/products.py) — these endpoints are REAL.
 *
 * The `Brief*` view-model types describe the Glance surface (UX §3). These are
 * now composed from REAL endpoints (lib/api/brief.ts); the only residual gap is
 * the shipped-item title/source detail (no deliverable-read endpoint yet). See
 * lib/api/placeholder.ts for the remaining fallback data.
 */

// ── Wire shapes (REAL endpoints) ──────────────────────────────────────────

/** `POST /api/auth/login` → backend SupabaseSession. */
export interface SupabaseSession {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  supabase_user_id: string;
  email: string | null;
}

/** `GET /api/v1/workspaces` element. */
export interface Workspace {
  id: string;
  name: string;
  region: string;
  safe_mode: boolean;
  created_at: string;
  updated_at: string;
}

/** `GET /api/v1/products` element. */
export interface Product {
  id: string;
  workspace_id: string;
  name: string;
  slug: string;
  repo_url: string | null;
  created_at: string;
  updated_at: string;
}

/** `RunStatus` (backend/execution/db.py) — the run lifecycle vocabulary. */
export type RunStatus = "open" | "running" | "review_ready" | "shipped" | "failed" | "cancelled";

/** `GET /api/v1/runs` element (backend RunResponse). */
export interface Run {
  id: string;
  workspace_id: string;
  product_id: string | null;
  request_id: string | null;
  status: RunStatus;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/messages` body — founder-direct submission. */
export interface MessageCreate {
  text: string;
  product_id?: string;
}

/** `POST /api/v1/messages` → 202 acceptance receipt (backend MessageAccepted). */
export interface MessageAccepted {
  accepted: boolean;
  duplicate: boolean;
  workspace_id: string;
}

/** `GET /api/v1/decisions` element (backend ProposalResponse). The decisions
 *  surface is the canonicalization proposal queue; `action_path` is the
 *  human-readable handle for what the proposal touches. */
export interface Proposal {
  id: string;
  proposal_kind: string;
  action_kind: string;
  action_path: string;
  status: string;
  score: number | null;
  created_at: string;
  expires_at: string | null;
}

/** `GET /api/v1/safemode/queue` element (backend SafeModeItemResponse). */
export interface SafeModeItem {
  id: string;
  workspace_id: string;
  deliverable_id: string;
  status: string;
  compensation_tier: string | null;
  expires_at: string;
  extension_count: number;
  created_at: string;
}

/** `POST /api/v1/safemode/{id}/{approve,deny}` → backend SafeModeActionResponse.
 *  `dispatched` is true only on approve (the held delivery is sent out). */
export interface SafeModeActionResponse {
  item_id: string;
  status: string;
  dispatched: boolean;
}

// ── Brief view-model (UX §3.3 lane states) ────────────────────────────────

export type LaneState = "working" | "needs-you" | "triggered" | "shipped" | "idle";

/** One product, as a calm plain-language status lane (never a metric card). */
export interface ProductLane {
  id: string;
  slug: string;
  name: string;
  state: LaneState;
  /** Plain-language status line — "writing tests for related-posts · 4m in". */
  status: string;
}

/** How a "Needs you" item can be resolved in-place. Only Safe-Mode held
 *  deliveries are resolvable from the PWA today (approve / deny endpoints exist,
 *  PR #17). Canonicalization proposals have no PWA resolve endpoint yet, so they
 *  carry no `resolve` and stay read-only. */
export interface SafeModeResolve {
  kind: "safemode";
  /** The raw Safe-Mode item id for /api/v1/safemode/{itemId}/{approve,deny}. */
  itemId: string;
}

/** A genuine fork that needs the founder (top "Needs you" strip + amber lane).
 *  `resolve` is present iff the item is actionable in-place; absent → read-only. */
export interface NeedsYouItem {
  id: string;
  productSlug: string;
  question: string;
  resolve?: SafeModeResolve;
}

export type ArtifactType = "pr" | "doc" | "image" | "slides" | "file" | "email";

/** A recently-shipped deliverable, polymorphic by artifact type (UX §4). */
export interface ShippedItem {
  id: string;
  title: string;
  productSlug: string;
  /** Where it landed — "GitHub PR #15", "Notion page", "Figma frame". */
  source: string;
  artifactType: ArtifactType;
  /** Proof verdict; for now always the calm "This is verified". */
  verdict: string;
}

/** The whole Glance surface.
 *
 * `placeholder` is true only while some field shown is still demo / not-yet-
 * served data. After the real-data wiring (brief.ts) the lanes, needs-you, and
 * recently-shipped all come from live endpoints, so `placeholder` is false on a
 * real read — even when the workspace is empty (that renders calm empty states,
 * NOT demo data). It flips back to true only if a hard failure forces the demo
 * fallback, or for the shipped-item *title/source* detail that has no endpoint
 * yet (derived from the run, not a deliverable-title read). */
export interface BriefView {
  needsYou: NeedsYouItem[];
  lanes: ProductLane[];
  recentlyShipped: ShippedItem[];
  placeholder: boolean;
}
