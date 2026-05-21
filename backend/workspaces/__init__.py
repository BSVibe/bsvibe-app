"""Workspace + Product entities — Workflow §3 first-class types."""

from __future__ import annotations

from backend.workspaces.db import (
    ProductRow,
    WorkspaceRow,
    WorkspacesBase,
)

__all__ = ["ProductRow", "WorkspaceRow", "WorkspacesBase"]
