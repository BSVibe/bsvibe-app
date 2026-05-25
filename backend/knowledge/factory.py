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
    from backend.knowledge.graph.restricted import RestrictedPluginGarden
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.graph.writer import GardenWriter
    from backend.knowledge.retrieval.canon_retriever import CanonConceptRetriever


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

    __slots__ = ("_context", "_vault_root", "_vault", "_writer")

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
        self._writer: GardenWriter | None = None

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
            from backend.knowledge.graph.vault import Vault as _Vault  # noqa: PLC0415

            self._vault_root.mkdir(parents=True, exist_ok=True)
            self._vault = _Vault(self._vault_root)
        return self._vault

    def writer(self) -> GardenWriter:
        """Return (or construct) a workspace-scoped :class:`GardenWriter`.

        The writer hangs off the same Vault as :meth:`vault`, so paths are
        already constrained to ``<vault_root>/<region>/<workspace_id>/``.
        Audit emit + sync_manager + ontology + event_bus default to None;
        request-handler glue injects them when needed.
        """
        if self._writer is None:
            from backend.knowledge.graph.writer import GardenWriter as _GW  # noqa: PLC0415

            self._writer = _GW(vault=self.vault())
        return self._writer

    def restricted_garden(self) -> RestrictedPluginGarden:
        """Return a read+seed-only wrapper for plugin/MCP callers.

        Per Workflow §6 #2 + §6 #5: external surfaces never see the raw
        ``GardenWriter`` — they get this wrapper so they can't mutate
        garden notes without going through ``IngestCompiler``.
        """
        from backend.knowledge.graph.restricted import (  # noqa: PLC0415
            RestrictedPluginGarden as _R,
        )

        return _R(writer=self.writer())

    def retriever(self) -> CanonConceptRetriever:
        """Return a workspace-scoped read-only canon retriever (Workflow §1.2).

        Satisfies the :class:`~backend.execution.verifier.service.CanonRetriever`
        Protocol: ``retrieve_for_signals(signals) -> list[str]`` surfaces THIS
        workspace's promoted active concepts relevant to a change's signals (top
        of the recurrence-gated registry, capped) so the verifier can fold them
        in as judge criteria. Reads only this workspace's vault storage (rooted
        at :attr:`vault_path`); an empty/unknown workspace yields ``[]`` and the
        retriever never raises into the verify path. Construction is cheap (no
        deps forced) — the derived index is built lazily per retrieval call.
        """
        from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415
        from backend.knowledge.retrieval.canon_retriever import (  # noqa: PLC0415
            CanonConceptRetriever,
        )

        return CanonConceptRetriever(FileSystemStorage(self._vault_root))
