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
  /** Lift A v2 — repo-bootstrap lifecycle marker.
   *
   *  `null` on every product created without a `repo_url`. Carries one of
   *  `pending` / `cloning` / `analyzing` / `ingesting` / `complete` /
   *  `failed:clone` / `failed:too_large` / `failed:ingest` while the
   *  background job runs. The detail page renders a calm BootstrapStatusPanel
   *  for every non-null, non-`complete` value. */
  bootstrap_status: string | null;
  bootstrap_artifacts_count: number | null;
  bootstrap_error: string | null;
  /** Lift E9 — per-chunk progress snapshot during ingest.
   *
   *  `null` outside the ingest window or on legacy rows. Shape:
   *  `{ chunks_done, chunks_total, chunks_failed, notes_created,
   *  notes_updated, phase }`. The Brief / BootstrapStatusPanel render
   *  `chunks_done / chunks_total (N failed)` as a small line next to the
   *  status pill — gives forward-motion visibility on a ~50-chunk bootstrap
   *  that previously showed the same opaque "ingesting" for an hour. */
  bootstrap_progress: BootstrapProgress | null;
  created_at: string;
  updated_at: string;
}

/** Per-chunk progress snapshot for a Product whose bootstrap is mid-ingest.
 *  Mirrors the backend's `bootstrap_progress` JSON column written by the
 *  bootstrap runtime's event subscriber. Every counter is monotonic across
 *  the compile_batch run. */
export interface BootstrapProgress {
  chunks_done: number;
  chunks_total: number;
  chunks_failed: number;
  notes_created: number;
  notes_updated: number;
  phase: string;
}

/** `GET /api/v1/products/{id}/bootstrap` body — progress snapshot. */
export interface ProductBootstrap {
  product_id: string;
  status: string | null;
  artifacts_count: number | null;
  error: string | null;
  run_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  /** Lift E9 — per-chunk progress snapshot while ingest is running.
   *
   *  `null` outside the ingest window or before any chunk has finished.
   *  Surfaced by the BootstrapStatusPanel as a small "Ingesting N / M chunks
   *  (K failed)" line beside the status pill so a long bootstrap shows
   *  forward motion. */
  progress: BootstrapProgress | null;
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

/** `OutputMode` (backend Workflow §3) — the binding's delivery knob. `safe`
 *  queues a verified Deliverable into the Safe Mode queue for founder approval
 *  (default for non-founder triggers); `direct` ships it straight out. */
export const OUTPUT_MODES = ["safe", "direct"] as const;
export type OutputMode = (typeof OUTPUT_MODES)[number];

/** The trigger knob — `{ enabled, filters }`. Mirrors the backend
 *  `TriggerKnob` schema 1:1 (extra=forbid). `enabled=false` is the safe
 *  default: a fresh binding doesn't auto-fire until the founder flips it on. */
export interface TriggerKnob {
  enabled: boolean;
  filters: Record<string, unknown>;
}

/** `GET /api/v1/products/{id}/bindings` element — the per-Product × Connector
 *  3-knob binding (backend ResourceBindingResponse). `selection`, `trigger`,
 *  and `output_mode` are the three founder-set knobs; `resource_id` is the
 *  connector-side identifier (e.g. a GitHub `"bsvibe/bsvibe-site"`). */
export interface ResourceBinding {
  id: string;
  workspace_id: string;
  product_id: string;
  connector_account_id: string;
  resource_id: string;
  selection: Record<string, unknown>;
  trigger: TriggerKnob;
  output_mode: OutputMode;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/products/{id}/bindings` body (backend ResourceBindingCreate,
 *  extra=forbid). Only `connector_account_id` + `resource_id` are required;
 *  the three knobs all have safe defaults (selection `{}`, trigger disabled
 *  no-filters, output_mode `safe`). */
export interface ResourceBindingCreate {
  connector_account_id: string;
  resource_id: string;
  selection?: Record<string, unknown>;
  trigger?: TriggerKnob;
  output_mode?: OutputMode;
}

/** `PATCH /api/v1/products/{id}/bindings/{binding_id}` body. Every knob is
 *  individually optional — pass only the knob being changed. */
export interface ResourceBindingUpdate {
  selection?: Record<string, unknown>;
  trigger?: TriggerKnob;
  output_mode?: OutputMode;
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
  /** The founder's Direction for this run (backend RunResponse.intent, from the
   *  run payload); `null` when the run carries none. */
  intent: string | null;
  /** L8 — a SHORT plain-language title of the task (frame stage). Review
   *  surfaces prefer this over the raw, developer-y `intent`. `framed_intent`
   *  is the retroactive fallback for runs framed before L8. Optional on the
   *  wire so pre-L8 fixtures keep validating. */
  summary_title?: string | null;
  framed_intent?: string | null;
  /** L9 — when the run was last restarted (founder retry); the elapsed-time
   *  surface counts from here instead of `created_at` so a retried run's clock
   *  resets. `null`/absent for a run that has never been retried. */
  restarted_at?: string | null;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/runs/{id}/cancel` body (backend RunCancelResponse) — the run
 *  is now `cancelled` (recoverable via `retryRun`). */
export interface RunCancel {
  id: string;
  status: RunStatus;
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

/** D6 — one mid-loop partial Deliverable (Synthesis §13 / Workflow §1). One
 *  external artifact the agent emitted via `emit_deliverable` BEFORE the
 *  verified terminal — a PR, a Notion page, a comment, a draft. The Run-view
 *  renders these in a streaming list, distinct from the verified-final the
 *  founder taps for the Delivery Report. */
export interface RunPartialDeliverable {
  id: string;
  artifact_type: string;
  summary: string | null;
  channel: string | null;
  external_ref: string | null;
  created_at: string;
}

/** `GET /api/v1/runs/{id}/detail` body (backend RunDetailResponse) — the
 *  inspectable run-detail surface (Stitch "Triggered"): status, trigger
 *  context, paused-run decisions, the latest verification outcome, the
 *  resulting Deliverable id (so the UI can link to its Delivery Report), and the
 *  run's activity timeline (the STORY of what the agent did). `timeline_source`
 *  is `"activities"` when real activity rows drive it, or `"derived"` when it's
 *  synthesized from the deliverable + verification we already carry.
 *
 *  D6 — `deliverable_id` is the run's verified-FINAL Deliverable; mid-loop
 *  partial Deliverables are surfaced separately in `partial_deliverables`
 *  (oldest-first, the order they were emitted). A run with zero mid-loop
 *  emits keeps the prior shape exactly (empty list). */
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
  partial_deliverables: RunPartialDeliverable[];
  activities: RunActivity[];
  timeline_source: "activities" | "derived";
  /** L2 (#9) — WHY a terminal-failed run failed (the latest FAILED / CANCELLED
   *  transition reason). `null` for non-failed runs. */
  failure_reason: string | null;
}

/** `POST /api/v1/runs/{id}/retry` body (backend RunRetryResponse) — the run is
 *  back to `open` for another attempt (L2 #9). */
export interface RunRetry {
  id: string;
  status: RunStatus;
  retry_count: number;
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
  /** Backend-authoritative trust signal (B4): `true` ONLY when a PASSED
   *  VerificationResult exists for the producing run — never inferred from the
   *  deliverable existing. The "This is verified" badge MUST derive from this. */
  verified: boolean;
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
  /** The founder's Direction that led to this work (from the producing run's
   *  payload); `null` when the run carries no recorded intent. */
  request: string | null;
  /** Backend-authoritative trust signal (B4): `true` ONLY when a PASSED
   *  VerificationResult is among the run's recorded verifications. The report's
   *  "verified" verdict derives from THIS flag, not from the list contents, so a
   *  hollow deliverable (none, or only failed/inconclusive) reads as needs
   *  review — never a green "verified". */
  verified: boolean;
  verifications: VerificationReportItem[];
  /** R8 — the producing run's status (e.g. "review_ready" / "shipped"), so the
   *  report footer can mirror the Brief: a held delivery shows Approve & ship /
   *  Decline; only a shipped run shows Rollback. `null` when the run is gone. */
  run_status?: string | null;
  /** R8 — the id of the PENDING Safe-Mode held delivery for this deliverable, if
   *  any. When set, the footer offers Approve & ship / Decline on this item
   *  (same as the Brief card); `null` when nothing is held. */
  held_delivery_item_id?: string | null;
  /** G2 "근거 포함 답변": the BSage knowledge the agent referenced for this work
   *  — promoted canon patterns, prior resolved decisions, and prior rejections
   *  folded into the verify contract. Deduped, first-seen order. Empty when
   *  nothing was retrieved (never a fabricated reference). */
  references: string[];
  /** R1 — a plain-language "what this did" composed (chat model) from the
   *  intent + captured diff; cached on the deliverable on first view. The
   *  redesigned report LEADS with this; `null` falls back to `request`. */
  narrative: string | null;
  /** R10/R12 — the knowledge this run WROTE: the notes it added to the vault,
   *  distinct from `references` (what it consulted). Each carries a de-slugged
   *  `title` + the vault-relative `path` so the "추가한 지식" chip deep-links to the
   *  note viewer. Empty until the settle drain runs (the group is then omitted). */
  written?: WrittenNote[];
}

/** R12 — one note this run added: a readable title + its vault-relative path,
 *  used to deep-link the report's "추가한 지식" chip to the note viewer. */
export interface WrittenNote {
  title: string;
  path: string;
}

/** R12 — one vault note's full content, for the report's note viewer. */
export interface KnowledgeNote {
  path: string;
  title: string;
  content: string;
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

/** `GET /api/v1/deliverables/{id}/diff` body (backend DeliverableDiffResponse) —
 *  the run's captured old↔new changes as a unified `git diff` patch (captured at
 *  verify-time for product runs). `diff` is `null` for a deliverable with no
 *  captured diff (a non-product/Direct run, or a row produced before the feature)
 *  — the viewer falls back to rendering content as additions. `truncated` flags
 *  the diff exceeded the stored cap (only the leading part is returned). */
export interface DeliverableDiff {
  diff: string | null;
  truncated: boolean;
}

/** One per-handle compensation outcome in a retract response (backend
 *  RetractedCompensationEntry). `plugin` is the originating plugin (e.g.
 *  `github`), `artifact_type` the action that was reversed (e.g. `pr`), and
 *  `output` the plugin's free-form compensate result. */
export interface RetractCompensationEntry {
  plugin: string;
  artifact_type: string;
  output: Record<string, unknown>;
}

/** `POST /api/v1/deliverables/{id}/retract` body (backend RetractResponse) —
 *  the outcome of rolling a shipped deliverable back via per-connector
 *  compensation (close the PR, delete the message, archive the page).
 *  `already_retracted` is `true` when the row was retracted before this call
 *  (200 idempotent no-op); `compensated` lists what was reverted on a first
 *  successful retract. A `400` (no captured handles → nothing to revert) or a
 *  `502` (a compensate dispatch failed) surfaces as an `ApiError`. */
export interface RetractResult {
  deliverable_id: string;
  retracted: boolean;
  retracted_at: string;
  already_retracted: boolean;
  compensated: RetractCompensationEntry[];
}

/** One node in a product repo's `main` tree (backend FileTreeEntryResponse).
 *  `path` is the full repo-relative path; `name` is the leaf; `kind` is the
 *  git object kind mapped to file/dir. */
export interface FileTreeEntry {
  name: string;
  path: string;
  kind: "file" | "dir";
}

/** `GET /api/v1/products/{id}/files/content` (ProductFileContentResponse) —
 *  one file's content from the product main checkout, capped + binary-aware. */
export interface ProductFileContent {
  path: string;
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

/** `POST /api/v1/messages/ask` → inline Direct-question answer (backend
 *  AskResponse). `answered: false` → the text is work (or no chat model) →
 *  dispatch via `submitMessage` instead. */
export interface AskResult {
  answered: boolean;
  answer: string | null;
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

/** L-D2 — one-click action spec on an executor B2b Decision (backend
 *  `DecisionAction`). Label fields ship per supported locale so the PWA
 *  renders them client-side without a server round-trip per locale change. */
export interface CheckpointAction {
  key: string;
  label_en: string;
  label_ko: string;
}

/** `GET /api/v1/checkpoints` element (backend CheckpointResponse). A paused-run
 *  Decision the founder must answer to resume a stuck run. `question` is the
 *  blocking prompt; `rationale` is the agent's optional why; `options` (B11a)
 *  are LLM-suggested choices (L-D1: not a closed set — founder may override
 *  via "Other" free-text); `actions` (L-D2) are one-click side-effecting
 *  buttons (ship / discard) on executor B2b Decisions. */
export interface Checkpoint {
  id: string;
  run_id: string;
  decision: string;
  question: string;
  options: string[] | null;
  actions: CheckpointAction[] | null;
  rationale: string | null;
  /** G4 (proposal §5.5): the founder's relevant already-resolved decisions
   *  ("Prior decision — Q: … A: …"), matched by signal overlap. Empty when
   *  nothing similar was decided before — show consistency, never invent it. */
  prior_decisions: string[];
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
  /** B12a — per-Run grouping key (Workflow §1.2). When multiple delivery rows
   *  share the same runId, the Decisions surface offers an "Approve all (N)"
   *  shortcut that hits POST /api/v1/safemode/runs/{runId}/approve. ``null``
   *  for legacy items emitted before the run_id column existed. */
  runId: string | null;
  /** Review context (joined from runs/deliverables/products by the aggregator)
   *  so the founder sees WHAT is being shipped, concisely, and can open the
   *  proof — instead of approving a generic "a delivery is held" blind. */
  deliverableId?: string | null;
  title?: string | null;
  productSlug?: string;
  detailHref?: string | null;
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
  /** L-D1 — LLM-suggested choices (or null for pure free-text). The PWA
   *  shows them as radio buttons + an "Other" option that reveals a
   *  free-text textarea. The backend accepts ANY non-empty string verbatim. */
  options: string[] | null;
  /** L-D2 — one-click actions (ship / discard) on executor B2b Decisions
   *  (verification_failed / human_review_required). When present, the row
   *  renders dedicated action buttons that POST ``action_key`` instead of
   *  ``answer`` for side-effecting resolutions. */
  actions: CheckpointAction[] | null;
  /** The Decision kind (e.g. "verification_failed", "human_review_required",
   *  "ask_user_question") — exposed so the UI can label / style by kind. */
  decision: string;
  rationale: string | null;
  /** G4 (proposal §5.5): the founder's relevant already-resolved decisions, so
   *  a recurring choice is answered consistently. Empty when none overlap. */
  priorDecisions: string[];
  /** Review context joined from the run/deliverable so the checkpoint reads as
   *  "<task title> · <product>" with a link to the proof, not a bare question. */
  runId?: string | null;
  title?: string | null;
  productSlug?: string;
  detailHref?: string | null;
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

// ── Unified Resolved list (frontend aggregation, mirror of Pending) ────────
// The Decisions "Resolved" tab is the history side of the same three queues: a
// resolved canon decision (the audit-log note), a decided Safe-Mode delivery
// (approved/denied/expired), and an answered paused-run checkpoint. Folded
// client-side exactly like the Pending list, newest-resolved first.

/** A resolved canon decision (the audit-trail note). */
export interface ResolvedKnowledge {
  kind: "knowledge";
  /** Stable list key — `decision-<path>`. */
  id: string;
  /** Directional decision recorded (e.g. `must-link` / `cannot-link`). */
  decisionKind: string;
  resolvedAt: string;
}

/** A decided Safe-Mode delivery (approved / denied / expired). */
export interface ResolvedDelivery {
  kind: "delivery";
  /** Stable list key — `delivery-<safemode item id>`. */
  id: string;
  itemId: string;
  /** Terminal outcome: `approved` | `denied` | `expired`. */
  status: string;
  /** Review context (joined from runs/deliverables/products by the aggregator)
   *  so the resolved row says WHAT was decided + links to the proof — mirroring
   *  the PENDING delivery row, instead of a blind generic "delivery approved". */
  title?: string | null;
  productSlug?: string;
  detailHref?: string | null;
  resolvedAt: string;
}

/** An answered paused-run checkpoint — the question + the founder's answer. */
export interface ResolvedCheckpointDecision {
  kind: "decision";
  /** Stable list key — `checkpoint-<checkpoint id>`. */
  id: string;
  checkpointId: string;
  question: string;
  resolution: string | null;
  resolvedAt: string;
}

/** One row in the unified Resolved list — a discriminated union mirroring
 *  {@link PendingDecision}. */
export type ResolvedDecision = ResolvedKnowledge | ResolvedDelivery | ResolvedCheckpointDecision;

/** `GET /api/v1/safemode/resolved` element (backend SafeModeResolvedResponse).
 *  A decided Safe-Mode delivery; `decided_at` is when it was settled. */
export interface SafeModeResolvedItem {
  id: string;
  deliverable_id: string;
  status: string;
  decided_at: string | null;
  created_at: string;
}

/** `GET /api/v1/checkpoints/resolved` element (backend
 *  ResolvedCheckpointResponse). An answered paused-run checkpoint. */
export interface ResolvedCheckpointItem {
  id: string;
  run_id: string;
  question: string;
  resolution: string | null;
  resolved_at: string | null;
}

/** `GET /api/v1/safemode/queue` element (backend SafeModeItemResponse). */
export interface SafeModeItem {
  id: string;
  workspace_id: string;
  deliverable_id: string;
  /** B12a — per-Run grouping key (Workflow §1.2). Nullable for legacy items
   *  that pre-date the run_id column. */
  run_id: string | null;
  status: string;
  compensation_tier: string | null;
  expires_at: string;
  extension_count: number;
  created_at: string;
}

/** `GET /api/v1/safemode/queue/by-run` element (backend SafeModeRunGroupResponse).
 *  B12a — pending Safe Mode items grouped by Run. */
export interface SafeModeRunGroup {
  run_id: string | null;
  items: SafeModeItem[];
}

/** `POST /api/v1/safemode/runs/{run_id}/approve` → backend
 *  SafeModeRunApproveResponse. B12a — per-Run bulk approve result. */
export interface SafeModeRunApproveResponse {
  run_id: string;
  approved_count: number;
  dispatched_count: number;
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
  /** The full note body (leading H1 dropped, capped ~8KB). `truncated` flags an
   *  overflow so the inspector can render it as a readable note. */
  body: string;
  truncated: boolean;
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

// ── Ontology retraction / correction (REAL endpoints, Lift M3a backend) ────

/** The action a `RetractionSignal` carries — one of `retract` (queue a tombstone
 *  write after the 30s undo window) or `correct` (queue a field rewrite). The
 *  literal mirrors `backend/knowledge/domain/retraction.py:OntologyAction`. */
export type OntologyAction = "retract" | "correct";

/** Mirror of backend `RetractionSignal` (extra=forbid, frozen). Returned by the
 *  retract/correct endpoints inside `RetractResponse.signal`. Identity
 *  (`id`/`workspace_id`/`actor_id`) is the idempotency key; `apply_at` is the
 *  server-stamped deadline before which `/corrections/{id}/undo` is honored.
 *
 *  `id` is the `correction_id` the undo endpoint expects (UUID-as-string over
 *  the wire). `apply_at` / `issued_at` are ISO-8601 strings server-stamped at
 *  intake. `reason` is the optional founder-typed free text (max 280 chars).
 */
export interface RetractionSignal {
  id: string;
  workspace_id: string;
  actor_id: string;
  node_ref: string;
  action: OntologyAction;
  issued_at: string;
  apply_at: string;
  reason: string | null;
  source: string;
}

/** `POST /api/v1/inside/nodes/{node_ref}/retract` + `…/correct` response (backend
 *  `RetractResponse`, extra=forbid). The issued signal + an idempotency flag
 *  (`created=false` when re-POSTing the same `correction_id`) + the undo window
 *  in seconds the UI uses to render the countdown. */
export interface RetractResponse {
  signal: RetractionSignal;
  created: boolean;
  undo_window_seconds: number;
}

/** `POST /api/v1/inside/corrections/{id}/undo` response (backend `UndoResponse`).
 *  Terminal status the toast renders into "Restored" / "Undo window expired"
 *  / "Already…". `not_found` is surfaced as an `ApiError` 404 by the wire
 *  layer and never lands here. */
export type UndoStatus = "undone" | "expired" | "already_applied" | "already_undone";

export interface UndoCorrectionResponse {
  correction_id: string;
  status: UndoStatus;
}

/** `POST /api/v1/inside/nodes/{node_ref}/retract` body (backend `RetractRequest`,
 *  extra=forbid). Both fields optional; `correction_id` lets clients retry
 *  safely (idempotency key). `reason` is founder-typed free text (max 280
 *  chars; design Q2 locks low-friction optional). */
export interface RetractRequestBody {
  correction_id?: string;
  reason?: string;
}

/** `POST /api/v1/inside/nodes/{node_ref}/correct` body (backend `CorrectRequest`,
 *  extra=forbid). `corrections` is a whitelisted field → new-value mapping
 *  the writer applies on apply_at — M3b PWA surfaces only `question` /
 *  `answer` / `body` per design §3.3. */
export interface CorrectRequestBody {
  correction_id?: string;
  reason?: string;
  corrections?: Record<string, string>;
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
 *  it is an outbound-only delivery builder (backend/workflow/application/delivery/connector_dispatch
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
  // Lift B — inbound knowledge-import connectors. These have NEITHER an
  // inbound webhook parser NOR an outbound delivery builder; the backend
  // validator recognises them via the kind map
  // (`backend.connectors.kinds.CONNECTOR_KINDS`). Their binding's
  // `delivery_config` carries the import-time config (vault_path /
  // export_path / api_token + page_id) rather than outbound routing.
  "obsidian",
  "claude",
  "gpt",
] as const;

export type ConnectorName = (typeof KNOWN_CONNECTORS)[number];

/** Mirror of `backend.connectors.kinds.CONNECTOR_KINDS` — the founder UI
 *  branches the create form (inbound config fields vs outbound delivery
 *  JSON) and the connector row's "Import now" button on this map.
 *
 *  Keep in sync with the backend; a mismatch causes the form to render
 *  the wrong fields (and the import endpoint to 422 the founder's tap). */
export type ConnectorKind = "inbound" | "outbound" | "both";

export const CONNECTOR_KINDS: Record<ConnectorName, ConnectorKind> = {
  github: "outbound",
  slack: "both",
  telegram: "outbound",
  discord: "outbound",
  sentry: "outbound",
  "email-sender": "outbound",
  obsidian: "inbound",
  claude: "inbound",
  gpt: "inbound",
  notion: "both",
};

/** Connectors that expose a bulk-import action via
 *  `POST /api/v1/connectors/{id}/import`. Mirrors the backend's
 *  `INBOUND_IMPORT_ACTIONS` keys. Used by `ConnectorRow` to decide
 *  whether to show "Import now" — `slack` is kind="both" but its inbound
 *  is webhook-driven, so it's NOT in this set (the backend would 422 on
 *  it). */
export const CONNECTORS_WITH_IMPORT: readonly ConnectorName[] = [
  "obsidian",
  "claude",
  "gpt",
  "notion",
] as const;

export function isImportableConnector(name: string): boolean {
  return (CONNECTORS_WITH_IMPORT as readonly string[]).includes(name);
}

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
  /** Lift B — the connector's classification (inbound / outbound / both).
   *  `null` for an unrecognised connector — the validator would normally
   *  reject it before reaching here, so callers can treat this as
   *  effectively always present for a known connector. Optional on the
   *  wire so legacy clients (and pre-Lift-B fixtures) keep validating. */
  kind?: ConnectorKind | null;
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
  /** Lift B — connector kind so the row UI can branch (Import-now button
   *  for inbound/both, hidden otherwise). `null` for an unrecognised
   *  connector (defensive). Optional on the wire so legacy fixtures
   *  (pre-Lift-B) keep validating. */
  kind?: ConnectorKind | null;
  /** ISO timestamp of the last successful import, or `null` if the
   *  binding has never been imported. Surfaced as "Last imported …" in
   *  the row's detail line. */
  last_import_at?: string | null;
  /** Count from the last successful import (notes / conversations /
   *  pages depending on connector). `null` until the first import. */
  last_import_count?: number | null;
  /** Lift 1 — for oauth2 connectors (github, …): the connected account's
   *  `@login` / workspace name once an OAuth token is bound, else `null`
   *  (not connected → the card shows "Connect with X"). Never the token. */
  oauth_account_label?: string | null;
  /** Lift E46 — `true` when the bound OAuth token row was flipped to
   *  `needs_reauth` by the backend (the refresh-token endpoint
   *  rejected the latest refresh attempt). The card surfaces a
   *  "Reconnect" CTA in this state instead of the steady "Connected"
   *  badge so the founder sees a dead credential immediately rather
   *  than waiting for the next dispatch to fail silently. */
  needs_reauth?: boolean;
}

/** `POST /api/v1/connectors/{id}/import` response (backend
 *  `ConnectorImportResult`). `imported_count` is the connector-agnostic
 *  count normalised on the server; `detail` is the raw per-connector
 *  summary the plugin's import action returned (notes_count /
 *  scanned_count for obsidian; pages_count / blocks_count for notion;
 *  etc.) — surfaced unchanged so the PWA can show a connector-specific
 *  breakdown without per-connector backend re-shaping. */
export interface ConnectorImportResult {
  imported_count: number;
  last_import_at: string;
  detail: Record<string, unknown>;
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

// ── Brief / Work-Home view-model ──────────────────────────────────────────
//
// Decisions are NOT modelled here — they live in the dedicated Decisions tab
// (Safe-Mode held deliveries + paused-run checkpoints + canon proposals). The
// Brief used to duplicate the Safe-Mode "Needs you" strip; that duplication was
// removed, so the old NeedsYouItem / SafeModeResolve view-models are gone.

export type ArtifactType = "pr" | "doc" | "image" | "slides" | "file" | "email";

/** A recently-shipped deliverable, polymorphic by artifact type (UX §4). */
export interface ShippedItem {
  id: string;
  title: string;
  productSlug: string;
  /** Where it landed — "GitHub PR #15", "Notion page", "Figma frame". */
  source: string;
  artifactType: ArtifactType;
  /** Proof verdict, derived from the backend-authoritative `Deliverable.verified`
   *  flag (B4): the calm "This is verified" ONLY when a PASSED VerificationResult
   *  backs it, else an honest "Awaiting verification" — never a hollow green. */
  verdict: string;
  /** External landing URL (`Deliverable.artifact_uri`) when one exists; absent
   *  for in-repo / unaddressed artifacts. The RecentlyShipped component does not
   *  render this yet — surfacing it as a tap target is a follow-up chunk. */
  link?: string;
}

/** Calm status tones for a run's lifecycle — color is used ONLY to carry
 *  status meaning (UX §5). `neutral` is the quiet grey for open/cancelled,
 *  `working` the soft ink for in-flight, `review` the amber "needs you",
 *  `shipped` the green "done", `failed` the muted red. Shared by the Work-Home
 *  surface (lib/runs/status.ts) and the product-detail run rows. */
export type ActivityTone = "neutral" | "working" | "review" | "shipped" | "failed";

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
  /** The run's Direction (or its deliverable's concise summary) so a run row
   *  says WHAT it was, not just a status. Null when the run carries no intent. */
  title: string | null;
  /** Where the row links — the deliverable proof when one exists, else the run.
   *  Makes "Needs your review" rows openable instead of a dead status line. */
  detailHref: string | null;
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
/** One actively-running piece of work for the "Working on now" hero — the
 *  founder's "what is BSVibe doing right now". Sourced from a run in an
 *  in-flight status (open / running). */
export interface ActiveWork {
  runId: string;
  /** What the run is doing — the founder's Direction (run.intent); `null` when
   *  the run carries none, so the component shows a calm i18n fallback. */
  title: string | null;
  productSlug: string;
  /** Lifecycle status (open = just started, running = working). The component
   *  translates this to a label + tone (keeps the data layer locale-free). */
  status: RunStatus;
  /** ISO timestamp the run started (created_at) — drives the "Nm in" elapsed. */
  startedAt: string;
}

/** One row in the unified Work Stream (the merged Brief/Activity history) — a
 *  completed (or in-flight) run rendered with its status as the lead signal.
 *  The component maps `status` → label + tone via i18n. */
export interface WorkStreamItem {
  runId: string;
  /** A concise one-line title: the shipped deliverable's summary when there is
   *  one, else the run's Direction; `null` → component shows an i18n fallback. */
  title: string | null;
  productSlug: string;
  status: RunStatus;
  /** Writer-stamped `updated_at`, ISO string (drives the relative time). */
  updatedAt: string;
  /** The deliverable this run produced (for the "View report" link), when one
   *  exists; `null` for runs that shipped nothing addressable. */
  deliverableId: string | null;
  artifactType: ArtifactType | null;
}

/** The unified Brief / Work-Home view-model: what needs the founder NOW, what
 *  BSVibe is doing, and the full chronological work stream.
 *
 *  R4 — decisions are UNIFIED back into the Brief: `needsYou` carries the
 *  pending Safe-Mode held deliveries + paused-run checkpoints (the same item
 *  shape DeliveryRow / CheckpointRow consume, joined to the run/deliverable for
 *  a concise title + proof link). A decision is an inline STATE of a work-stream
 *  resolved HERE with context — not a divorced inbox tab. This supersedes L7
 *  (#6), which had removed the needs-you block to avoid duplicating the separate
 *  Decisions tab; the duplication is gone the other way now — decisions live in
 *  the Brief. */
export interface BriefView {
  /** Pending items the founder must judge, resolved inline in the Brief
   *  (deliveries + checkpoints). Empty when nothing needs a call. */
  needsYou: PendingDecision[];
  working: ActiveWork[];
  stream: WorkStreamItem[];
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
  /** Lift E4 — ISO 8601 last-heartbeat timestamp, `null` until the worker
   *  daemon has heartbeated at least once. Surfaces "Last seen" on the card. */
  last_heartbeat: string | null;
  /** Lift E13 — mirrors the predicate `find_available_worker` uses (last
   *  heartbeat within 120s). `status="online"` can lie when the worker
   *  process died before clearing the column; this flag is the source of
   *  truth for "can this worker actually take work right now". A row with
   *  `status="online", heartbeat_fresh=false` is the stale-online diagnosis. */
  heartbeat_fresh: boolean;
  /** Lift E4 — ISO 8601 row-creation timestamp ("Added on" detail). */
  created_at: string | null;
}
