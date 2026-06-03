"""Shared helpers for D2 MCP tool handlers."""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow

logger = structlog.get_logger(__name__)


async def workspace_region(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    """Resolve the ``region`` for ``workspace_id``, falling back to the default."""
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        return get_settings().knowledge_default_region
    return row.region


def vault_root_for(*, region: str, workspace_id: uuid.UUID) -> Path:
    """Return the on-disk vault root for one (region, workspace_id)."""
    settings = get_settings()
    return Path(settings.knowledge_vault_root) / region / str(workspace_id)


__all__ = [
    "vault_root_for",
    "workspace_region",
]
