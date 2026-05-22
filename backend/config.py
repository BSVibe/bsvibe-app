"""Application settings — pydantic-settings, env-loaded.

All vars use the ``BSVIBE_`` prefix. Reads ``.env`` when present.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import metadata
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    git_sha: str = "dev"
    version: str = _resolve_version()

    # Supabase IdP (Workflow §2.1) — the backend calls GoTrue directly for
    # login / OAuth code exchange / refresh / logout. JWT *verification* is
    # configured separately in backend.shared.authz.settings (USER_JWT_*).
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Default region stamped onto workspaces created at signup (§10.2).
    default_workspace_region: str = "us-1"

    # Sandbox settings (backend.supervisor.sandbox)
    sandbox_enabled: bool = False
    docker_host: str = ""
    sandbox_image: str = "bsvibe-sandbox:latest"
    sandbox_idle_reap_seconds: int = 1800
    sandbox_max_concurrent: int = 2

    # Gateway settings (backend.gateway)
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

    # Skills settings (backend.skills) — per-workspace skill directory.
    # Layout: ``<skills_root>/<workspace_id>/*.md`` per Workflow §6 #5.
    skills_root: str = "var/skills"

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
