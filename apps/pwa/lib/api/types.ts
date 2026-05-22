/**
 * Wire + view-model types for the PWA.
 *
 * The `Supabase*`, `Workspace`, and `Product` shapes mirror the backend
 * response models 1:1 (backend/api/auth/routes.py, backend/api/v1/
 * workspaces.py, backend/api/v1/products.py) — these endpoints are REAL.
 *
 * The `Brief*` view-model types describe the Glance surface (UX §3). Some of
 * the data behind them is not yet served by the backend; see
 * lib/api/placeholder.ts for exactly which fields are placeholder.
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

/** A genuine fork that needs the founder (top "Needs you" strip + amber lane). */
export interface NeedsYouItem {
  id: string;
  productSlug: string;
  question: string;
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

/** The whole Glance surface. `placeholder` is true while any lane status,
 *  needs-you item, or shipped item is still demo data (see brief.ts). */
export interface BriefView {
  needsYou: NeedsYouItem[];
  lanes: ProductLane[];
  recentlyShipped: ShippedItem[];
  placeholder: boolean;
}
