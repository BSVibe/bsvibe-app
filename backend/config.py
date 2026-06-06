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

    # Sandbox settings (backend.workflow.infrastructure.sandbox)
    sandbox_enabled: bool = False
    docker_host: str = ""
    sandbox_image: str = "bsvibe-sandbox:latest"
    sandbox_idle_reap_seconds: int = 1800
    sandbox_max_concurrent: int = 2

    # Gateway settings (backend.router)
    # 32-byte AES-256-GCM key, base64-url-encoded. Generate with:
    # `python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
    gateway_kms_key_b64: str = ""
    # Default 2-tier classifier thresholds (used by LocalVsCloudClassifier
    # when no override is supplied).
    gateway_local_score_max: int = 40
    gateway_cloud_score_min: int = 60

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
    # giving up (→ system_error). Default 30 min — a CLI coding agent run is
    # long-lived. Operator-tunable per deployment.
    executor_task_timeout_s: float = 1800.0

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
