/**
 * Wire + view-model types for the PWA.
 *
 * The `Supabase*`, `Workspace`, and `Product` shapes mirror the backend
 * response models 1:1 (backend/api/auth/routes.py, backend/api/v1/
 * workspaces.py, backend/api/v1/products.py) — these endpoints are REAL.
 *
 * The `Brief*` view-model types describe the Glance surface (UX §3). These are
 * now composed entirely from REAL endpoints (lib/api/brief.ts): lanes, needs-
 * you, AND recently-shipped (the last from /api/v1/deliverables). The only
 * residual fallback is the demo lanes shown when the core read fails mid-load
 * (see lib/api/placeholder.ts).
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

/** `DeliverableType` (backend/execution/db.py) — the artifact kind a verified
 *  run produced. The PWA maps these → the calmer `ArtifactType` UI vocabulary. */
export type DeliverableType = "code" | "pr" | "page" | "page_image" | "direct_output";

/** `GET /api/v1/deliverables` element (backend DeliverableResponse). A real
 *  artifact produced by a verified run; `summary`/`artifact_refs` come from the
 *  orchestrator's free-form payload (may be null/empty), `artifact_uri` is the
 *  external landing spot (PR URL, page URL, …) when one exists. */
export interface Deliverable {
  id: string;
  run_id: string;
  workspace_id: string;
  deliverable_type: DeliverableType;
  summary: string | null;
  artifact_refs: string[];
  artifact_uri: string | null;
  created_at: string;
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

/** `GET /api/v1/checkpoints` element (backend CheckpointResponse). A paused-run
 *  Decision the founder must answer to resume a stuck run. `question` is the
 *  blocking prompt; `rationale` is the agent's optional why. */
export interface Checkpoint {
  id: string;
  run_id: string;
  decision: string;
  question: string;
  rationale: string | null;
  created_at: string;
}

/** `POST /api/v1/checkpoints/{id}/resolve` → backend ResolveResponse. The
 *  Decision is recorded and the paused run is resumed (RUNNING → OPEN). */
export interface CheckpointResolveResponse {
  id: string;
  run_id: string;
  status: string;
  resolution: string;
  resolved_at: string;
  run_status: RunStatus;
}

/** One linked action's apply outcome inside an AcceptResponse (backend
 *  ApplyResultResponse). */
export interface ApplyResult {
  action_path: string;
  final_status: string;
  affected_paths: string[];
  error: string | null;
}

/** `POST /api/v1/decisions/{proposal_path}/accept` → backend AcceptResponse. */
export interface AcceptResponse {
  proposal_path: string;
  status: string;
  results: ApplyResult[];
}

/** `POST /api/v1/decisions/{proposal_path}/reject` → backend RejectResponse. */
export interface RejectResponse {
  proposal_path: string;
  status: string;
  reason: string | null;
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

/** `GET /api/v1/inside/concepts` element (backend ConceptResponse). One
 *  canonical anchor — a settled concept on the founder's knowledge "wall".
 *  `summary` is a short body excerpt (empty for a freshly-promoted anchor that
 *  carries only its title); `alias_count` is the cheap connectedness signal
 *  (how many variant spellings resolve onto this anchor). Mirrors the backend
 *  model field-for-field (backend/api/v1/inside.py). */
export interface Concept {
  id: string;
  name: string;
  summary: string;
  aliases: string[];
  alias_count: number;
  created_at: string;
  updated_at: string;
}

/** `GET /api/v1/inside/observations` element (backend ObservationResponse). One
 *  recent garden observation — a raw, unpromoted settle note the SettleWorker
 *  deposited. `captured_at` is the writer-stamped deposit date (may be absent
 *  for a note written without one). Mirrors the backend model 1:1. */
export interface Observation {
  id: string;
  title: string;
  excerpt: string;
  tags: string[];
  captured_at: string | null;
}

// ── Connectors (REAL endpoint /api/v1/connectors) ─────────────────────────

/** The connector names the backend's `ConnectorCreate.connector` validator
 *  accepts today (backend/api/v1/connectors.py): a name is registerable iff it
 *  has an inbound parser (ConnectorInboundResolver._PARSERS — github / slack /
 *  telegram / discord / sentry) OR an outbound delivery builder
 *  (OUTBOUND_EVENT_BUILDERS — notion / slack / email-sender). Anything else
 *  422s. We mirror that exact validated set so the picker never offers a
 *  connector the server rejects.
 *
 *  Note: the email connector's backend name is `email-sender` (NOT `email`) —
 *  it is an outbound-only delivery builder (backend/delivery/connector_dispatch
 *  .py OUTBOUND_EVENT_BUILDERS), so it has no inbound parser but is registerable
 *  via the outbound branch of the validator.
 *
 *  Note: linear / trello are named in the Workflow as eventual connectors but
 *  have NEITHER an inbound parser NOR an outbound builder wired yet, so the
 *  create validator rejects them — they are intentionally absent here until the
 *  backend lands their mappers (see PR description gap note). */
export const KNOWN_CONNECTORS = [
  "github",
  "slack",
  "telegram",
  "discord",
  "sentry",
  "notion",
  "email-sender",
] as const;

export type ConnectorName = (typeof KNOWN_CONNECTORS)[number];

/** `POST /api/v1/connectors` body (backend ConnectorCreate, extra=forbid).
 *  `delivery_config` is founder-set outbound routing config (e.g. notion
 *  `{ "parent_page_id": … }`) — never derived from LLM/work output; it defaults
 *  to `{}` on the wire when the connector is inbound-only. */
export interface ConnectorCreate {
  connector: string;
  signing_secret: string;
  external_ref?: string | null;
  delivery_config?: Record<string, unknown>;
}

/** `POST /api/v1/connectors` → 201 (backend ConnectorCreated). The ONLY place
 *  the `webhook_token` + full `webhook_url` are ever returned — like an API
 *  key, shown once. Mirrors the backend model field-for-field. */
export interface ConnectorCreated {
  id: string;
  connector: string;
  external_ref: string | null;
  is_active: boolean;
  created_at: string;
  delivery_config: Record<string, unknown>;
  webhook_token: string;
  webhook_url: string;
}

/** `GET /api/v1/connectors` element (backend ConnectorOut). Never the secret,
 *  never the full token — `token_hint` is the last-4 mask (`...abcd`). */
export interface Connector {
  id: string;
  connector: string;
  external_ref: string | null;
  is_active: boolean;
  created_at: string;
  delivery_config: Record<string, unknown>;
  token_hint: string;
}

// ── Model accounts (REAL endpoint /api/v1/accounts) ────────────────────────

/** The data-jurisdiction allow-list the backend ModelAccount schema accepts
 *  (backend/accounts/schemas.py `Jurisdiction`). The picker mirrors it exactly
 *  so the form never offers a value the create validator 422s on. */
export const MODEL_ACCOUNT_JURISDICTIONS = ["us", "eu", "kr", "local", "unknown"] as const;

export type ModelAccountJurisdiction = (typeof MODEL_ACCOUNT_JURISDICTIONS)[number];

/** `POST /api/v1/accounts` body (backend ModelAccountCreate, extra=forbid). The
 *  caller supplies the plaintext `api_key`; the service encrypts it at rest and
 *  the response NEVER echoes it back (only `has_api_key: true`). Field set
 *  mirrors the backend schema 1:1 — `api_base` / `extra_params` are optional. */
export interface ModelAccountCreate {
  provider: string;
  label: string;
  litellm_model: string;
  api_key: string;
  data_jurisdiction: ModelAccountJurisdiction;
  api_base?: string | null;
  extra_params?: Record<string, unknown>;
}

/** `PATCH /api/v1/accounts/{id}` body (backend ModelAccountUpdate, extra=forbid)
 *  — every field optional. Used for activate / deactivate (`is_active`) and any
 *  field edit. A new `api_key` rotates the stored credential; it is never read
 *  back. */
export interface ModelAccountUpdate {
  label?: string;
  litellm_model?: string;
  api_base?: string | null;
  api_key?: string;
  data_jurisdiction?: ModelAccountJurisdiction;
  is_active?: boolean;
  extra_params?: Record<string, unknown>;
}

/** `GET /api/v1/accounts` element / `POST` 201 (backend ModelAccountOut). Never
 *  exposes the encrypted key — `has_api_key` is the masked "a credential is on
 *  file" flag. Mirrors the backend response model field-for-field. */
export interface ModelAccount {
  id: string;
  workspace_id: string;
  account_id: string;
  provider: string;
  label: string;
  litellm_model: string;
  api_base: string | null;
  data_jurisdiction: ModelAccountJurisdiction;
  is_active: boolean;
  has_api_key: boolean;
  extra_params: Record<string, unknown>;
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
  /** External landing URL (`Deliverable.artifact_uri`) when one exists; absent
   *  for in-repo / unaddressed artifacts. The RecentlyShipped component does not
   *  render this yet — surfacing it as a tap target is a follow-up chunk. */
  link?: string;
}

/** The whole Glance surface.
 *
 * `placeholder` is true only while some field shown is demo / not-yet-served
 * data. The lanes, needs-you, and recently-shipped all come from live endpoints
 * (recently-shipped from /api/v1/deliverables), so `placeholder` is false on a
 * real read — even when the workspace is empty (that renders calm empty states,
 * NOT demo data). It flips back to true only if a hard failure forces the demo
 * lane fallback. */
export interface BriefView {
  needsYou: NeedsYouItem[];
  lanes: ProductLane[];
  recentlyShipped: ShippedItem[];
  placeholder: boolean;
}
