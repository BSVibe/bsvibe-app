"""KnowledgeFactory — per-workspace, per-region constructor for knowledge components.

Workspace scoping is enforced at the Vault path layer: every component
(``GardenWriter``, ``IngestCompiler``, ``VaultRetriever``,
``CanonicalizationService``) hangs off a ``Vault`` rooted at
``<vault_root>/<region>/<workspace_id>/``. Downstream methods do not need a
per-call ``workspace_id`` argument — the bound Vault already constrains every
read and write to that workspace.

Request-handler glue (Bundle API / Bundle G) is expected to:

1. Extract ``workspace_id`` from the verified Supabase JWT
2. Pick the ``region`` from the ``Workspace.region`` column (defaults to
   :data:`backend.config.Settings.knowledge_default_region` Phase 1)
3. Construct a ``KnowledgeFactory`` per request
4. Pull pre-scoped components from the factory and inject them into plugin
   / skill / MCP contexts

The factory holds no shared mutable state; one instance per request is the
intended pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Identifies the workspace + region a knowledge factory is bound to."""

    region: str
    workspace_id: str


class KnowledgeFactory:
    """Per-workspace, per-region factory.

    Currently exposes a single ``vault()`` accessor; further accessors for
    ``GardenWriter`` / ``IngestCompiler`` / ``VaultRetriever`` /
    ``CanonicalizationService`` land alongside their concrete construction
    helpers (deferred until each component's dependency graph is finalized
    in the integration phase).
    """

    __slots__ = ("_context", "_vault_root", "_vault")

    def __init__(
        self,
        *,
        region: str,
        workspace_id: str,
        vault_root: Path,
    ) -> None:
        self._context = WorkspaceContext(region=region, workspace_id=workspace_id)
        self._vault_root = vault_root / region / workspace_id
        self._vault: Vault | None = None

    @property
    def context(self) -> WorkspaceContext:
        return self._context

    @property
    def vault_path(self) -> Path:
        """Filesystem root for this workspace's vault."""
        return self._vault_root

    def vault(self) -> Vault:
        """Return (or construct) the workspace-scoped ``Vault``."""
        if self._vault is None:
            from backend.knowledge.graph.vault import Vault as _Vault

            self._vault_root.mkdir(parents=True, exist_ok=True)
            self._vault = _Vault(self._vault_root)
        return self._vault
