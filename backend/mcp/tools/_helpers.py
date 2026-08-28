"""Shared helpers for D2 MCP tool handlers."""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog

from backend.knowledge.graph.vault_paths import workspace_vault_root

logger = structlog.get_logger(__name__)


def vault_root_for(*, workspace_id: uuid.UUID) -> Path:
    """The on-disk vault root for one workspace.

    Thin re-export of the ONE definition
    (:func:`backend.knowledge.graph.vault_paths.workspace_vault_root`) so the
    existing MCP callers keep their import. There used to be a sibling
    ``workspace_region`` here that read ``WorkspaceRow.region`` — a second
    answer to "where does this workspace's vault live", which every REST route
    answered differently. Region is a deployment constant; see the module
    docstring of ``vault_paths``.
    """
    return workspace_vault_root(workspace_id)


__all__ = [
    "vault_root_for",
]
