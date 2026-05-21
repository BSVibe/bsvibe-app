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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
