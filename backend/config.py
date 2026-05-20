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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
