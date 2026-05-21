"""Knowledge module — vault graph, canonicalization, ingest, retrieval, MCP.

Workspace scoping is enforced at the :class:`KnowledgeFactory` layer, which
constructs per-workspace ``Vault`` instances rooted at
``<vault_root>/<region>/<workspace_id>/``.
"""

from __future__ import annotations

from backend.knowledge.factory import KnowledgeFactory, WorkspaceContext

__all__ = ["KnowledgeFactory", "WorkspaceContext"]
