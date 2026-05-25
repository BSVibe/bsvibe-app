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

/** `POST /api/v1/products` body (backend ProductCreate, extra=forbid). The slug
 *  must match `^[a-z][a-z0-9-]*$` (the backend's _SLUG_RE) — the create form
 *  auto-suggests + validates one before submit. `repo_url` is optional and is
 *  omitted from the wire body when blank rather than sent as an empty string. */
export interface ProductCreate {
  name: string;
  slug: string;
  repo_url?: string | null;
}

// ── Product resources (REAL endpoint /api/v1/products/{id}/resources) ──────

/** The resource kinds the Add-resource picker offers. Backend-side `kind` is a
 *  free short string (`String(32)`), so this is a UI convenience set, not a
 *  server-enforced enum — anything in this list (or any short tag) is accepted.
 *  `link` is the calm catch-all default. */
export const RESOURCE_KINDS = ["link", "repo", "doc", "deploy", "note"] as const;

export type ResourceKind = (typeof RESOURCE_KINDS)[number];

/** `GET /api/v1/products/{id}/resources` element / `POST` 201 (backend
 *  ResourceResponse, extra=forbid). A named pointer the product works with — a
 *  repo, doc, deploy, or free note. `url` / `note` are both optional. Mirrors
 *  the backend response model field-for-field. */
export interface ProductResource {
  id: string;
  product_id: string;
  workspace_id: string;
  /** Short free-string tag the UI renders as a chip (link / repo / doc / …). */
  kind: string;
  title: string;
  url: string | null;
  note: string | null;
  created_at: string;
}

/** `POST /api/v1/products/{id}/resources` body (backend ResourceCreate,
 *  extra=forbid). `kind` + `title` are required (title non-blank); `url` must be
 *  an http(s):// or mailto: link when present, and both `url` and `note` are
 *  omitted from the wire body when blank rather than sent as empty strings. */
export interface ProductResourceCreate {
  kind: string;
  title: string;
  url?: string | null;
  note?: string | null;
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

/** The "outside" that asked for this run (backend RunTriggerContext) — pulled
 *  defensively out of the run's free-form payload. Each field is null when the
 *  payload doesn't carry it (a sparse run shows a calm minimal detail). */
export interface RunTriggerContext {
  source: string | null;
  trigger_kind: string | null;
  intent_text: string | null;
  product: string | null;
}

/** One paused-run Decision on a run-detail response (backend RunDecision). The
 *  founder resolves a `pending` decision via POST /api/v1/checkpoints/{id}/resolve. */
export interface RunDecision {
  id: string;
  decision: string;
  question: string;
  rationale: string | null;
  status: "pending" | "resolved";
  resolution: string | null;
  created_at: string;
}

/** The latest VerificationResult outcome for a run (backend RunVerification). */
export interface RunVerification {
  id: string;
  outcome: VerificationOutcome;
  created_at: string;
}

/** One meaningful event on the run's timeline — the STORY of what the agent did
 *  (backend RunActivity). `type` is the raw ExecutionRunActivity activity_type
 *  (`tool_call` / `verify` / `settle` / `error`) or a synthesized `deliver` when
 *  the timeline is derived; `label` is a short human summary. */
export interface RunActivity {
  type: string;
  label: string;
  created_at: string;
}

/** `GET /api/v1/runs/{id}/detail` body (backend RunDetailResponse) — the
 *  inspectable run-detail surface (Stitch "Triggered"): status, trigger
 *  context, paused-run decisions, the latest verification outcome, the
 *  resulting Deliverable id (so the UI can link to its Delivery Report), and the
 *  run's activity timeline (the STORY of what the agent did). `timeline_source`
 *  is `"activities"` when real activity rows drive it, or `"derived"` when it's
 *  synthesized from the deliverable + verification we already carry. */
export interface RunDetail {
  id: string;
  workspace_id: string;
  product_id: string | null;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  trigger: RunTriggerContext;
  decisions: RunDecision[];
  verification: RunVerification | null;
  deliverable_id: string | null;
  activities: RunActivity[];
  timeline_source: "activities" | "derived";
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
  /** Commit / diff URL when the producing run recorded one; absent otherwise. */
  diff_url?: string | null;
  created_at: string;
}

/** `VerificationOutcome` (backend/execution/db.py) — the verdict of running the
 *  declared verification contract against a run. */
export type VerificationOutcome = "passed" | "failed" | "inconclusive";

/** One `VerificationResult` in a delivery report (backend VerificationReport).
 *  `contract` is the work LLM's declared checks (the checks BSVibe promised to
 *  run) and `result` is the execution outcome of running them — both free-form
 *  JSON, rendered defensively by the report view. */
export interface VerificationReportItem {
  id: string;
  outcome: VerificationOutcome;
  contract: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
}

/** `GET /api/v1/deliverables/{id}/report` body (backend
 *  DeliverableReportResponse) — the glass-box proof for one shipped
 *  deliverable: the artifact plus the verification(s) recorded for its run. */
export interface DeliverableReport {
  deliverable: Deliverable;
  verifications: VerificationReportItem[];
}

/** `GET /api/v1/deliverables/{id}/artifacts/{ref:path}` body (backend
 *  ArtifactContentResponse) — the produced CONTENT of one artifact file, read
 *  from the persisted run workspace. `content` is the file as UTF-8 text
 *  (lossy `errors="replace"`), capped at 256 KiB. `truncated` flags the file
 *  exceeded the cap (only the leading bytes are returned). `binary` flags a
 *  non-text file, where `content` is a short "binary file, N bytes" note rather
 *  than raw bytes. Mirrors the backend response model field-for-field. */
export interface ArtifactContent {
  ref: string;
  content: string;
  truncated: boolean;
  binary: boolean;
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

/** `GET /api/v1/decisions/log` element (backend DecisionResponse). One resolved
 *  decision-memory note — the founder-approval audit trail. `decision_kind` is
 *  the directional decision recorded (e.g. `must-link` / `cannot-link`);
 *  `proposal_id` links back to the proposal it resolved (when known). */
export interface DecisionLogEntry {
  id: string;
  proposal_id: string | null;
  decision_kind: string;
  actor_id: string | null;
  created_at: string;
}

// ── Unified Decisions queue (frontend aggregation of three real queues) ────
// The Decisions surface is the SINGLE place for everything needing the
// founder's judgment. It aggregates three EXISTING backend queues client-side
// into one calm list, each item tagged by `kind` so the row renders the right
// resolve affordance wired to its OWN endpoint:
//   - "delivery"  → Safe Mode held delivery  (/api/v1/safemode/{id}/approve|deny)
//   - "decision"  → paused-run checkpoint     (/api/v1/checkpoints/{id}/resolve)
//   - "knowledge" → canon proposal            (/api/v1/decisions/{path}/accept|reject)
// This is the SAME set the Brief "Needs you" strip surfaces for the overlapping
// kinds (deliveries + proposals); the Decisions surface additionally folds in
// paused-run checkpoints, which the Brief does not yet show.
export type PendingKind = "delivery" | "decision" | "knowledge";

/** A Safe Mode held delivery awaiting Approve / Deny. */
export interface PendingDelivery {
  kind: "delivery";
  /** Stable list key — `delivery-<safemode item id>`. */
  id: string;
  /** Raw Safe-Mode item id for /api/v1/safemode/{itemId}/{approve,deny}. */
  itemId: string;
  createdAt: string;
}

/** A paused-run checkpoint awaiting the founder's answer. */
export interface PendingCheckpoint {
  kind: "decision";
  /** Stable list key — `checkpoint-<checkpoint id>`. */
  id: string;
  /** Raw checkpoint id for /api/v1/checkpoints/{checkpointId}/resolve. */
  checkpointId: string;
  /** The agent's blocking question. */
  question: string;
  rationale: string | null;
  createdAt: string;
}

/** A canonicalization proposal awaiting Accept / Reject. Carries the raw
 *  `Proposal` so the existing detail panel keeps working unchanged. */
export interface PendingProposal {
  kind: "knowledge";
  /** Stable list key — `proposal-<proposal id/path>`. */
  id: string;
  proposal: Proposal;
  createdAt: string;
}

/** One row in the unified Pending list — a discriminated union over the three
 *  kinds the founder must judge. */
export type PendingDecision = PendingDelivery | PendingCheckpoint | PendingProposal;

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

/** `GET /api/v1/inside/concepts/{id}` related-concept (backend RelatedConcept).
 *  One neighbour of the inspected concept in the deterministic concept graph;
 *  `weight` is the co-occurrence weight (shared observations for a `co-occurs`
 *  edge, 1.0 for an `alias-of` link). Clickable to pivot the inspector onto the
 *  neighbour. Mirrors the backend model 1:1 (backend/api/v1/inside.py). */
export interface RelatedConcept {
  id: string;
  name: string;
  weight: number;
}

/** `GET /api/v1/inside/concepts/{id}` source observation (backend
 *  SourceObservation). One garden note whose tags resolve onto the inspected
 *  concept — its origin/usage. `captured_at` is the writer-stamped deposit date
 *  (may be absent). Mirrors the backend model 1:1. */
export interface ConceptSourceObservation {
  id: string;
  title: string;
  excerpt: string;
  captured_at: string | null;
}

/** `GET /api/v1/inside/concepts/{id}` response (backend ConceptDetailResponse).
 *  The read-only inspector behind a clicked concept: identity (`id` / `name` /
 *  `aliases`) plus its `related` graph neighbours (with weight) and the
 *  `observations` that reference it. Read-only — Stitch's Edit/Retract map to
 *  canonicalization actions with no v1 endpoint yet (deferred). Mirrors the
 *  backend model field-for-field. */
export interface ConceptDetail {
  id: string;
  name: string;
  aliases: string[];
  related: RelatedConcept[];
  observations: ConceptSourceObservation[];
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

/** `GET /api/v1/inside/graph` node (backend GraphNode). One entity in the
 *  force-directed knowledge graph: `id` is stable across edges, `label` the
 *  display name, `kind` the ontology entity type (may be absent; the TYPE
 *  legend colours by it), `community` the deterministic emergent-cluster id
 *  (may be absent; the COMMUNITY legend colours by it), `weight` the node's
 *  degree (connectedness signal for sizing). Mirrors the backend model
 *  field-for-field (backend/api/v1/inside.py). */
export interface KnowledgeGraphNode {
  id: string;
  label: string;
  kind?: string | null;
  community?: string | null;
  weight: number;
}

/** `GET /api/v1/inside/graph` edge (backend GraphEdge). `source`/`target` are
 *  `KnowledgeGraphNode` ids; `type` the relationship type (may be absent);
 *  `weight` the edge importance. Mirrors the backend model 1:1. */
export interface KnowledgeGraphEdge {
  source: string;
  target: string;
  type?: string | null;
  weight: number;
}

/** `GET /api/v1/inside/graph` response (backend GraphResponse). The workspace
 *  knowledge graph as nodes + edges for a force-directed view. An empty/sparse
 *  workspace returns `{ nodes: [], edges: [] }`. Edges only reference nodes
 *  present in `nodes`. */
export interface KnowledgeGraph {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
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
 *  (backend/accounts/schemas.py `Jurisdiction`). Invisible infra — the founder
 *  no longer hand-picks it (the backend defaults it to "unknown"); this stays
 *  as the source for the `ModelAccountJurisdiction` type the response carries. */
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
  /** Invisible infra — the founder no longer hand-picks this; the backend
   *  defaults it to "unknown". Optional for the rare explicit caller. */
  data_jurisdiction?: ModelAccountJurisdiction;
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

// ── Routing rules (Settings → Models → ROUTING) ───────────────────────────

/** One condition the routing engine evaluates. `field` is one of the
 *  evaluator's whitelisted ALLOWED_FIELDS (e.g. `classified_intent`). The UI
 *  only surfaces the value as "what a rule matches" — it does not edit complex
 *  conditions (deferred). Mirrors the backend ConditionResponse 1:1. */
export interface RuleCondition {
  condition_type: string;
  field: string;
  operator: string;
  value: unknown;
  negate: boolean;
}

/** POST-body condition (backend ConditionPayload). `negate` defaults to `false`
 *  server-side, so callers may omit it. */
export interface RuleConditionInput {
  condition_type: string;
  field: string;
  operator: string;
  value: unknown;
  negate?: boolean;
}

/** `GET /api/v1/rules` element / `POST` 201 (backend RuleResponse). A rule maps
 *  a unit of work to a `target_model`, ordered by `priority` (ascending wins
 *  first). `is_default` marks the catch-all; `is_active` toggles it. Mirrors the
 *  backend response model field-for-field. */
export interface RoutingRule {
  id: string;
  name: string;
  priority: number;
  target_model: string;
  is_default: boolean;
  is_active: boolean;
  conditions: RuleCondition[];
}

/** POST body for creating a routing rule (backend RuleCreate, extra=forbid).
 *  `conditions` is dropped from the wire when empty so a catch-all rule's body
 *  stays minimal; the backend defaults it to `[]`. */
export interface RoutingRuleCreate {
  name: string;
  target_model: string;
  priority: number;
  is_default?: boolean;
  is_active?: boolean;
  conditions?: RuleConditionInput[];
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

// ── Activity view-model (read-only run history surface) ───────────────────

/** Calm status tones for a run's lifecycle — color is used ONLY to carry
 *  status meaning (UX §5). `neutral` is the quiet grey for open/cancelled,
 *  `working` the soft ink for in-flight, `review` the amber "needs you",
 *  `shipped` the green "done", `failed` the muted red. */
export type ActivityTone = "neutral" | "working" | "review" | "shipped" | "failed";

/** One row in the Activity list: a real ExecutionRun rendered in plain
 *  language. `productSlug` is resolved via the run's `product_id` (degrades to
 *  "workspace" when the run carries none). `statusLabel` is the calm word the
 *  founder reads ("Shipped", "Needs your review"); `tone` drives the lone
 *  status colour. The raw `runId` is what the expand fetch narrows on. */
export interface ActivityRun {
  runId: string;
  productSlug: string;
  status: RunStatus;
  statusLabel: string;
  tone: ActivityTone;
  /** Writer-stamped `updated_at` (most recent activity), ISO string. */
  updatedAt: string;
}

/** One delivered artifact under an Activity run, mapped to the calm UI
 *  vocabulary. `verdict` is the constant "This is verified" (deliverables only
 *  exist for verified runs). `link` is the external landing spot
 *  (`Deliverable.artifact_uri`) when one exists. */
export interface ActivityDeliverable {
  id: string;
  /** First non-empty line of the summary, or a calm fallback. */
  title: string;
  artifactType: ArtifactType;
  /** Plain-language "where it landed" — "opened a pull request". */
  source: string;
  verdict: string;
  link?: string;
}

// ── Product detail view-model (focused per-product window) ────────────────

/** One run in a product's "Recent runs" list — the same calm vocabulary the
 *  Activity surface uses (plain-language status + lone status tone). `shipped`
 *  flags the runs whose delivered artifacts the detail view surfaces eagerly. */
export interface ProductDetailRun {
  runId: string;
  status: RunStatus;
  statusLabel: string;
  tone: ActivityTone;
  /** Writer-stamped `updated_at` (most recent activity), ISO string. */
  updatedAt: string;
  /** True iff the run shipped — its deliverables are surfaced under "Shipped". */
  shipped: boolean;
}

/** The focused per-product view-model (the `/products/[slug]` surface). Composed
 *  entirely client-side from the list endpoints: the product is found in
 *  /api/v1/products, its runs filtered out of /api/v1/runs by `product_id`, and
 *  each shipped run's deliverables fetched from /api/v1/deliverables?run_id=.
 *
 *  `currentStatus` is the plain-language headline derived from the product's
 *  latest run (or a calm "Nothing running yet" when it has none). `shipped` are
 *  the delivered artifacts across the product's shipped runs, newest first. */
export interface ProductDetailView {
  id: string;
  slug: string;
  name: string;
  /** External repo landing spot, when the product carries one. */
  repoUrl: string | null;
  /** Headline status line for the product header, in plain language. */
  currentStatus: string;
  /** Lone status tone for the header dot — derived from the latest run. */
  currentTone: ActivityTone;
  runs: ProductDetailRun[];
  shipped: ShippedItem[];
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

// ── Notifications (REAL endpoint /api/v1/notifications/prefs) ──────────────

/** Workspace notification preferences (backend PrefsBody, extra=forbid).
 *
 *  `matrix` is the events × channels enable grid keyed
 *  `event_id -> channel_id -> enabled`. The known event ids are
 *  needs_you / triggered / shipped / failed / daily_brief; the known channel
 *  ids are in_app / email / slack — the backend validator (and this surface)
 *  require exactly that grid. Quiet hours are `"HH:MM"` strings (the same shape
 *  the PWA <input type="time"> emits). v1 stores PREFERENCES only — real
 *  email/Slack send is a later phase, and per-product overrides from the design
 *  are intentionally omitted. */
export interface NotificationPrefs {
  matrix: Record<string, Record<string, boolean>>;
  quiet_hours_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
}

// ── Skills (REAL endpoint /api/v1/skills) ──────────────────────────────────
// The backend skills API carries reads (list + get), create (POST writes a new
// skill MD file under the per-workspace skills dir) AND update (PATCH rewrites
// the editable body fields), per backend/api/v1/skills.py. The PWA surfaces a
// Library + a New-skill create form + an editor (edit summary + system prompt).

/** `GET /api/v1/skills` element / `GET /api/v1/skills/{name}` / `POST` 201 /
 *  `PATCH` 200 (backend SkillResponse, extra=forbid). Skill manifest metadata
 *  for the workspace. Mirrors the backend response model field-for-field.
 *  `has_system_prompt` is the "a prompt is on file" flag; `system_prompt` is the
 *  raw Markdown body (empty string when none) — carried so the editor can
 *  round-trip it. */
export interface Skill {
  name: string;
  version: string;
  description: string;
  author: string;
  allowed_tools: string[];
  model: string | null;
  has_system_prompt: boolean;
  system_prompt: string;
}

/** `POST /api/v1/skills` body (backend SkillCreate, extra=forbid). `name` is the
 *  human-friendly handle (the backend slugifies it for the filename + manifest
 *  name); `summary` becomes the manifest description (the LLM match signal);
 *  `system_prompt` is the markdown body. CREATE only — no version/author/tools
 *  knobs in this lift. Mirrors the backend schema 1:1. */
export interface SkillCreate {
  name: string;
  summary: string;
  system_prompt: string;
}

/** `PATCH /api/v1/skills/{name}` body (backend SkillUpdate, extra=forbid). Only
 *  the editable body fields are mutable: `summary` (manifest description) and
 *  `system_prompt` (markdown body). The slug / `name` is immutable on update (a
 *  rename would mean a file rename — deferred), so it is NOT part of the body. */
export interface SkillUpdate {
  summary: string;
  system_prompt: string;
}

// ── Executor workers (REAL endpoint /api/v1/workers) ───────────────────────

/** `GET /api/v1/workers` element (backend WorkerResponse). A registered worker
 *  is a machine the founder runs the BSVibe worker process on, where their
 *  coding-agent CLIs (claude_code / codex / opencode) are logged in — letting
 *  BSVibe route work to those CLIs under the founder's own subscription.
 *
 *  `status` is heartbeat-driven ("online" / "offline"); `capabilities` lists the
 *  CLIs that machine can drive. NOTE: the response carries no `last_heartbeat`
 *  yet (a later backend tweak) — surface `status` + `capabilities`, not an exact
 *  last-seen timestamp. Mirrors the backend response model field-for-field. */
export interface Worker {
  id: string;
  workspace_id: string;
  name: string;
  labels: string[];
  capabilities: string[];
  status: string;
  is_active: boolean;
}

/** `POST /api/v1/workers/install-token` → `{ token }` (backend response). The
 *  ONLY place the plaintext install token is ever returned — like an API key,
 *  shown once. The founder feeds it to the worker process on its host. */
export interface WorkerInstallToken {
  token: string;
}
