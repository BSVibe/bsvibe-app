"""/api/v1/settings — read-only exposure of process settings.

Surfaces the safe subset of :class:`backend.config.Settings` so the
operator UI can render config without an SSH session. Secrets
(``gateway_kms_key_b64``, ``database_url`` etc.) are redacted.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from backend.config import get_settings

router = APIRouter()


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: Literal["dev", "staging", "prod"]
    version: str
    git_sha: str

    # Knowledge module
    knowledge_vault_root: str
    knowledge_default_region: str

    # Skills module
    skills_root: str

    # Sandbox (supervisor)
    sandbox_enabled: bool
    sandbox_image: str
    sandbox_idle_reap_seconds: int
    sandbox_max_concurrent: int


@router.get("")
async def get_settings_view() -> SettingsResponse:
    s = get_settings()
    return SettingsResponse(
        environment=s.environment,
        version=s.version,
        git_sha=s.git_sha,
        knowledge_vault_root=s.knowledge_vault_root,
        knowledge_default_region=s.knowledge_default_region,
        skills_root=s.skills_root,
        sandbox_enabled=s.sandbox_enabled,
        sandbox_image=s.sandbox_image,
        sandbox_idle_reap_seconds=s.sandbox_idle_reap_seconds,
        sandbox_max_concurrent=s.sandbox_max_concurrent,
    )
