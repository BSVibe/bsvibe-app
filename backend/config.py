"""Application settings — pydantic-settings, env-loaded.

All vars use the ``BSVIBE_`` prefix. Reads ``.env`` when present.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import metadata
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from backend.shared.core import csv_list_field, parse_csv_list

# The PWA (app.bsvibe.dev) calls the backend (api.bsvibe.dev) directly from
# the browser cross-origin. Default to the local PWA dev port so a bare local
# checkout works without extra env. Override in prod via the comma-separated
# ``BSVIBE_CORS_ALLOWED_ORIGINS`` env var.
_DEFAULT_CORS_ORIGINS: list[str] = ["http://localhost:3700"]


def _resolve_version() -> str:
    try:
        return metadata.version("bsvibe-app")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


class Settings(BaseSettings):
    """Runtime configuration for the BSVibe backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BSVIBE_",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
    # Two-role Postgres (B2b) — RLS is a REAL layer-3 backstop only when the
    # RUNTIME connection is a NON-superuser, NON-BYPASSRLS role. ``database_url``
    # is that runtime role (least-privilege ``bsvibe_app``). Migrations, which
    # need DDL + role/policy management, run as the OWNER role via
    # ``migration_database_url``. Empty (the default) means "single role for
    # both" — dev / SQLite / any deployment that has not cut over yet keeps
    # working unchanged: :meth:`migration_url` falls back to ``database_url``.
    migration_database_url: str = ""

    # DB connection safety-net (backend.data.engine). Postgres kills any session
    # left ``idle in transaction`` longer than this many milliseconds — the
    # ``idle_in_transaction_session_timeout`` GUC, applied via asyncpg
    # server_settings on EVERY app connection. A prod incident: leaked held-open
    # transactions (~15) exhausted the connection pool → every DB endpoint hung
    # → full outage needing a manual restart. Post-#632 the drive loop uses SHORT
    # transactions, so no legit app op holds a transaction idle more than a few
    # seconds; this only ever catches a LEAK and lets the pool self-heal. Default
    # 120s — comfortably longer than any legit short transaction, short enough to
    # auto-heal a leak fast. ``0`` DISABLES the guard.
    idle_in_transaction_session_timeout_ms: int = 120000

    redis_url: str = "redis://localhost:6387/0"
    environment: Literal["dev", "staging", "prod"] = "dev"

    # Worker trigger mode (backend.workers). DB-polling is the DEFAULT + tested
    # path: each worker periodically queries its source table. ``redis_streams``
    # is an OPT-IN scale/latency improvement (Workflow §12.5 #8) — producers
    # ALSO XADD a notification to the matching stream (best-effort, soft-fail;
    # the DB row stays the source of truth) and the worker daemon runs each
    # worker as a Redis Streams consumer (XREADGROUP → existing single-tick
    # handler → XACK) instead of the poll loop. Switching modes never changes
    # the business logic — Redis is only a different *trigger* for the same tick.
    worker_mode: Literal["db_polling", "redis_streams"] = "db_polling"
    git_sha: str = "dev"
    version: str = _resolve_version()

    # Supabase IdP (Workflow §2.1) — the backend calls GoTrue directly for
    # login / OAuth code exchange / refresh / logout. JWT *verification* is
    # configured separately in backend.shared.authz.settings (USER_JWT_*).
    supabase_url: str = ""
    # Supabase **publishable** key (``sb_publishable_...``), passed as the
    # GoTrue ``apikey`` header. Replaces the deprecated legacy ``anon`` key.
    supabase_publishable_key: str = ""
    # Default region stamped onto workspaces created at signup (§10.2).
    default_workspace_region: str = "us-1"

    # Embedded OAuth 2.0 authorization server (Lift D1, backend.identity.oauth_*).
    # PEM-encoded ECDSA P-256 private key used to sign OAuth access tokens.
    # When empty (local dev), an ephemeral keypair is generated at first
    # use — tokens are NOT portable across process restarts. Generate a
    # stable prod key with:
    #     openssl ecparam -genkey -name prime256v1 -noout -out oauth_key.pem
    # Then base64 the file contents into the env var (or paste the PEM
    # directly between quotes in .env).
    oauth_private_key_pem: str = ""
    # Issuer claim stamped onto issued JWTs and advertised in the RFC 8414
    # authorization-server metadata. Defaults to a sane local value;
    # set ``BSVIBE_OAUTH_ISSUER=https://api.bsvibe.dev`` in prod.
    oauth_issuer: str = "http://localhost:8000"

    # Connector OAuth — bsvibe acting as an OAuth *client* of third parties
    # (backend.connectors.auth). One App credential set per provider (standard
    # SaaS pattern: per-workspace *tokens*, not per-workspace apps).
    #
    # GitHub has NO env credentials: its App is created + stored entirely via
    # the in-app manifest flow ("Set up GitHub App" → encrypted DB row in
    # connector_oauth_app_credentials → loaded at startup). The DB is the single
    # source of truth. To create the App manually instead, see the README /
    # .env.example note — but there is no BSVIBE_GITHUB_APP_* runtime var.
    #
    # Vanilla OAuth2 connectors (authorization_code) — one App per provider,
    # registered from env when both id + secret are set (no manifest flow).
    # slack: bot OAuth v2; notion / discord: Basic-auth token exchange; sentry:
    # install→grant integration. (These will move to in-app DB setup in a
    # follow-up lift, matching github.)
    slack_client_id: str = ""
    slack_client_secret: str = ""
    notion_client_id: str = ""
    notion_client_secret: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    sentry_client_id: str = ""
    sentry_client_secret: str = ""

    # Agent shell_exec timeout (backend.workflow.infrastructure.tools). The
    # executor's ``shell_exec`` tool runs a command in the per-product DinD
    # sandbox and hard-kills it at this many seconds. It was a hardcoded 30s,
    # which killed a legit ``uv run pytest`` / ``uv sync`` on a real repo
    # mid-run — the agent then retried inside its turn until the whole-turn cap
    # (~30 min): the "30-minute flail". 900s (15 min) comfortably covers a test
    # suite / build while still bounding a hang. This is NOT a restriction on
    # WHAT the agent runs — long runs are already safe (per-turn drive-session
    # release holds no DB connection across the turn; the whole-turn cap still
    # bounds a runaway). An agent may request a LONGER per-call ``timeout_s`` for
    # a big suite, clamped to ``shell_exec_timeout_max_s`` so it can neither be
    # starved nor request infinity.
    shell_exec_timeout_s: float = 900.0
    # Hard ceiling on a per-call ``shell_exec(timeout_s=...)`` override — a
    # runaway cannot request more than this. 3600s (1h) matches the executor
    # task timeout ceiling.
    shell_exec_timeout_max_s: float = 3600.0

    # Verify-phase command timeouts (backend.workflow.application.verification_service).
    # A verify command check runs in the sandbox and is killed at
    # ``verify_command_timeout_s``; a DERIVED-gate command (the repo's own
    # test/quality command, e.g. ``uv run pytest``) at
    # ``verify_gate_command_timeout_s``. Both were hardcoded (60s / 300s), which
    # truncated a real test-suite gate. Raised + tunable so a slow but legit
    # suite runs to completion instead of a false timeout-fail.
    verify_command_timeout_s: float = 300.0
    verify_gate_command_timeout_s: float = 900.0

    # Sandbox settings (backend.workflow.infrastructure.sandbox)
    sandbox_enabled: bool = False
    docker_host: str = ""
    sandbox_image: str = "bsvibe-sandbox:latest"
    sandbox_idle_reap_seconds: int = 1800
    sandbox_max_concurrent: int = 2
    # Explicit ``--user`` for the per-project sandbox container. The worker
    # writes the run worktree as root (uid 0), so the sandbox image's default
    # uid-1000 user cannot write ``/work`` — set this to ``"0:0"`` to match.
    # Empty leaves the image default (no ``--user``); never a silent coercion.
    sandbox_user: str = ""

    # Per-product test-Postgres sidecar (GATED, default OFF). When enabled, each
    # sandbox is stood up alongside a blank ``pgvector/pgvector:pg16`` sidecar on
    # a DEDICATED user-defined bridge network ``sbxnet-<product>`` so the sandbox
    # reaches it by container-name DNS, and the ``sandbox_test_db_env`` vars are
    # injected into the sandbox. The user-defined network is the escape hatch:
    # the DinD firewall DROPs private-range traffic only on the DEFAULT bridge
    # (``-i docker0``), so a dedicated network is NOT subject to that rule. OFF
    # (the default) is byte-identical to no-sidecar: no network, no env, no
    # sidecar. The sidecar is a BLANK PG with a single superuser/owner role; the
    # PRODUCT's own migration chain CREATEs any runtime role — the platform does
    # NOT hardcode a product's role model. Defaults mirror bsvibe-app CI so the
    # dogfood target works out of the box; other products override or stay off.
    sandbox_test_db_enabled: bool = False
    sandbox_test_db_image: str = "pgvector/pgvector:pg16"
    sandbox_test_db_superuser: str = "bsvibe"
    sandbox_test_db_password: str = "bsvibe"  # noqa: S105 — blank-PG default, not a secret
    sandbox_test_db_name: str = "bsvibe"
    # Env vars injected into the SANDBOX container. ``{host}`` is substituted with
    # the sidecar's container-DNS name at create time. Env override form is JSON.
    sandbox_test_db_env: dict[str, str] = {
        "BSVIBE_DATABASE_URL": "postgresql+asyncpg://bsvibe_app:bsvibe_app_ci@{host}:5432/bsvibe",
        "BSVIBE_MIGRATION_DATABASE_URL": "postgresql+asyncpg://bsvibe:bsvibe@{host}:5432/bsvibe",
        "BSVIBE_APP_DB_PASSWORD": "bsvibe_app_ci",
    }
    # Command run INSIDE the sandbox (after venv sync) to migrate/provision the
    # test DB before tests. Empty = skip (the generic default). Dogfood sets e.g.
    # ``uv run alembic upgrade head`` (uses BSVIBE_MIGRATION_DATABASE_URL, above).
    sandbox_test_db_setup_cmd: str = ""
    sandbox_test_db_ready_timeout_s: float = 60.0

    # Gateway settings (backend.router)
    # 32-byte AES-256-GCM key, base64-url-encoded. Generate with:
    # `python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
    gateway_kms_key_b64: str = ""

    # Knowledge settings (backend.knowledge) — vault FS root + region.
    # Per-workspace vault lives at ``<knowledge_vault_root>/<region>/<workspace_id>/``.
    knowledge_vault_root: str = "var/vault"
    knowledge_default_region: str = "us-1"

    # Knowledge semantic-search embedding (the pgvector index DERIVED from the
    # Markdown source-of-truth, proposal §5.4). This is a DEPLOYMENT-level model
    # — set it once and every settled note is embedded into ``note_embeddings``
    # automatically, and queries embed against the same model. Empty disables
    # semantic search (the index simply isn't built; canon/decision/rejection
    # retrieval is unaffected). Distinct from the gateway's PER-ACCOUNT intent-
    # routing embedding config — knowledge search is not opt-in per workspace.
    # Example: ``ollama/bge-m3`` with ``knowledge_embedding_api_base`` pointing
    # at the local Ollama.
    knowledge_embedding_model: str = ""
    knowledge_embedding_api_base: str | None = None
    knowledge_embedding_timeout_s: float = 30.0

    # Lift E18 — how many ingest-compile chunks ``IngestCompiler.compile_batch``
    # processes in parallel per call. Each in-flight chunk consumes one slot
    # of the worker fleet's capacity-aware dispatch (E16). Tune to ``<= total
    # free worker slots`` (worker count × ``max_parallel_tasks_per_worker``)
    # so the worker fleet is the constraint, not us. Default matches the
    # worker's default ``max_parallel_tasks=3`` for a single-worker fleet.
    ingest_compile_parallelism: int = 3

    # Skills settings (backend.extensions.skill) — per-workspace skill directory.
    # Layout: ``<skills_root>/<workspace_id>/*.md`` per Workflow §6 #5.
    skills_root: str = "var/skills"

    # Worker runtime (backend.workflow.infrastructure.workers.run) — each ExecutionRun drives inside
    # ``<run_workspace_root>/<run_id>/``. The agent loop mounts this dir into
    # the sandbox; the work LLM's file writes land here.
    #
    # W1 onwards (when run.product_id is set), this dir is provisioned as a
    # ``git worktree`` of the product workspace's ``main`` branch — see
    # :mod:`backend.storage.product_workspace`.
    run_workspace_root: str = "var/runs"

    # W1 — product workspace root. Each ProductRow gets a canonical git repo
    # at ``<product_workspace_root>/<product_id>/`` on the ``main`` branch.
    # Per-run worktrees branch from this and merge back on ship.
    product_workspace_root: str = "var/products"

    # GitHub CI-green auto-merge (backend.workflow ... MergeWatchWorker). When on,
    # a PR opened by github delivery is watched and squash-merged once its CI is
    # green + mergeable, with per-repo serialization + conflict recovery. OFF keeps
    # the existing "open PR, human merges" behavior — nothing is auto-merged.
    github_auto_merge_enabled: bool = False

    # CI-green auto-merge poll cadence + CI deadline (used only when
    # ``github_auto_merge_enabled``). ``ci_deadline_s`` bounds how long a watched
    # PR may sit waiting for its checks to go green before the watch row is
    # FAILED (default 1h); ``poll_interval_s`` is the MergeWatchWorker's idle
    # loop cadence between claim passes (default 30s).
    github_auto_merge_ci_deadline_s: float = 3600.0
    github_auto_merge_poll_interval_s: float = 30.0

    # Conflict-robustness (MergeWatchWorker conflict recovery). When an auto-merge
    # hits a genuine conflict the run is re-dispatched to the agent to resolve it.
    # ``resolution_deadline_s`` bounds how long the row waits on an UNCHANGED PR
    # head (agent hasn't re-pushed) before the re-drive is presumed stalled/failed
    # (default 15min); ``max_redispatch`` caps how many times a single conflict
    # head is re-dispatched before the worker escalates to a founder
    # ``merge_conflict_review`` Decision instead of parking forever. Together they
    # guarantee conflict recovery terminates (retry → escalate), never wedges.
    github_conflict_resolution_deadline_s: float = 900.0
    github_conflict_max_redispatch: int = 2

    # Audit relay sink (backend.workers.relays) — the RelayWorker drains
    # ``audit_outbox`` into this HTTP endpoint when set. Empty (the default)
    # selects the no-sink ``LoggingRelay`` (drain + ack, no remote delivery).
    audit_relay_url: str = ""

    # Execution settings — agent loop budgets per Workflow §3 + memory
    # ``bsnexus-budget-handoff-design``. Operator may tune for local-LLM
    # vs frontier-model deployments; defaults match Cycle 7-14 dogfood
    # telemetry on qwen3-coder:30b.
    execution_work_round_budget: int = 48
    execution_prepare_round_budget: int = 3
    execution_verify_round_budget: int = 1
    execution_summarize_round_budget: int = 2
    # Soft-pressure handoff trigger: how many rounds before the
    # ``work`` budget cap the agent should be nudged toward summarize.
    execution_soft_pressure_headroom: int = 6
    # Decomposer cycle cap — caps planning/decomposer.py CoT depth.
    decomposer_cycle_cap: int = 14

    # Executor-pool dispatch (executor-pool Lift 5b). A run whose resolved
    # ModelAccount is ``provider='executor'`` dispatches a task to an external
    # CLI worker instead of running the native LLM loop; this is how long the
    # orchestrator waits for the worker to report a terminal result before
    # giving up (→ system_error). Default 1 h — a CLI coding agent turn can
    # legitimately run a cold ``uv sync`` + a large repo's FULL pytest suite
    # inline before reporting, which routinely exceeds 30 min. Long turns are
    # SAFE post-#632 (the drive loop holds no DB connection across the turn) /
    # #633 (idle-tx self-heal recovers any leak); the stale-claim reaper still
    # bounds a genuinely-crashed run at 2× this value. Operator-tunable per
    # deployment. NOTE: this is the ONLY knob the ``workflow.agent_loop.act``
    # caller (default_timeout_s=None) and the reaper lease derive from — every
    # other caller pins an explicit, shorter timeout, so raising this only
    # lengthens the act turn cap and widens the reaper lease with it.
    executor_task_timeout_s: float = 3600.0

    # Capacity-aware dispatch (Lift E16). Backend must NOT dispatch onto a
    # worker stream when the worker is already at its in-flight cap — the
    # worker's poll loop skips polling while ``len(in_flight) >=
    # max_parallel_tasks``, leaving newly-XADDed tasks unread until a slot
    # frees up. Pre-E16 the backend's ``await_completion`` timer started at
    # dispatch time, so it could expire before the worker ever read the
    # task, leading to false ``failed`` results on chunks the worker hadn't
    # touched (dogfood: bsvibe-app big-repo bootstrap). The default mirrors
    # the worker's ``WorkerSettings.max_parallel_tasks`` default so a stock
    # founder deployment is internally consistent; operators may override
    # both sides in lockstep.
    max_parallel_tasks_per_worker: int = 3

    # Lift E16 — bounded total wait when every worker in the workspace is
    # at capacity. ``ExecutorAdapter.chat`` loops with bounded retry waiting
    # for a free worker slot before dispatching; this caps the wait so a
    # genuinely under-provisioned / wedged workspace surfaces a meaningful
    # error instead of looping forever. Default 30 min matches the legacy
    # per-task timeout — beyond that the caller should see "no capacity"
    # as a hard signal, not a soft hang.
    executor_capacity_wait_max_s: float = 1800.0

    # Lift E16 — sleep between capacity-availability re-checks while the
    # adapter is awaiting a free worker slot. Short enough that a freshly
    # vacated slot is picked up promptly without flooding the DB with
    # ``find_available_worker`` calls.
    executor_capacity_wait_poll_s: float = 2.0

    # PWA origin — the browser app at https://app.bsvibe.dev. The OAuth
    # ``GET /api/oauth/authorize`` endpoint redirects the user agent to
    # ``<pwa_url>/oauth/consent`` so the consent screen renders inside the
    # PWA (where the Supabase session is reachable). Browser-driven OAuth
    # flows cannot carry a Bearer header through a top-level navigation;
    # hosting consent on the API origin would force every MCP client to
    # die at the consent step. Local dev default mirrors the PWA dev port.
    pwa_url: str = "http://localhost:3700"

    # CORS allow-list for the browser PWA calling the backend cross-origin.
    # ``Annotated[list[str], NoDecode]`` + a ``mode="before"`` validator opts
    # out of pydantic-settings' default JSON decode so a deployer can set
    # ``BSVIBE_CORS_ALLOWED_ORIGINS=https://app.bsvibe.dev,https://...`` as a
    # plain comma-separated string (mirrors backend.shared.core.csv_list_field,
    # the established list-from-env pattern used by FastApiSettings).
    #
    # NO explicit alias: an explicit ``validation_alias`` makes pydantic-settings
    # bypass ``env_prefix`` and read the bare name (``CORS_ALLOWED_ORIGINS``),
    # which silently ignored the documented ``BSVIBE_CORS_ALLOWED_ORIGINS`` in
    # prod. Letting the field name + ``env_prefix="BSVIBE_"`` resolve the env
    # var keeps it consistent with every other setting here.
    cors_allowed_origins: Annotated[list[str], NoDecode] = csv_list_field(
        default=_DEFAULT_CORS_ORIGINS,
        description="Comma-separated CORS allow_origins for the browser PWA.",
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_allowed_origins(cls, value: str | list[str] | None) -> list[str]:
        return parse_csv_list(value) or list(_DEFAULT_CORS_ORIGINS)

    def migration_url(self) -> str:
        """The DSN alembic connects with — the OWNER role (B2b).

        Falls back to :attr:`database_url` when ``migration_database_url`` is
        unset, so a single-role deployment (dev / SQLite / pre-cutover prod)
        keeps migrating as before. After cutover, ``database_url`` is the
        least-privilege runtime role and this returns the owner role that can
        run DDL and manage roles/policies.
        """
        return self.migration_database_url or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
