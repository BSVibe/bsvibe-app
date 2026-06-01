"""Per-workspace vault dependencies for the ``/api/v1/inside`` surface.

The three builders here all root at the SAME per-workspace vault path
(``<knowledge_vault_root>/<region>/<workspace_id>/``) — the same boundary the
canonicalization queue + promotion pipeline use, so the anchors / observations
read here are exactly the ones the trust ratchet built for THIS workspace, and
a vault outside it is structurally unreachable.

Overridable in tests via ``app.dependency_overrides`` to point at a fixture
vault. ``build_inside_storage`` + ``build_inside_index`` are re-exported from
the package ``__init__`` so test overrides keep their existing import paths.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import networkx as nx
from fastapi import Depends

from backend.api.deps import get_workspace_id

# ``_vault_root`` is the same helper :mod:`backend.api.v1.decisions` defines —
# importing it preserves the FS-as-SoT contract that every per-workspace
# knowledge surface addresses ONE root.
from backend.api.v1.decisions import _vault_root
from backend.knowledge.canonicalization.concept_graph import build_concept_graph
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend


async def build_inside_storage(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> StorageBackend:
    """Read-only vault storage rooted at the caller's per-workspace vault.

    Same per-workspace root the canonicalization queue + promotion pipeline
    write to (``<knowledge_vault_root>/<region>/<workspace_id>/`` via
    :func:`backend.api.v1.decisions._vault_root`), so the anchors and garden
    observations read here are exactly the ones the trust ratchet built for
    THIS workspace — a vault outside it is not addressable.

    Overridable in tests via ``app.dependency_overrides`` to point at a
    fixture vault.
    """
    vault_root = _vault_root(workspace_id)
    vault_root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(vault_root)


async def build_inside_index(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
) -> InMemoryCanonicalizationIndex:
    """Vault-derived canonicalization index for listing canonical anchors.

    Rebuilds from the workspace vault markdown alone (Handoff §10) — a pure
    read of the FS-as-SoT concept registry. Rooted at the same storage as
    :func:`build_inside_storage` so the index never sees another workspace's
    concepts.
    """
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return index


async def build_inside_graph(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
) -> nx.MultiDiGraph:
    """The caller's per-workspace knowledge graph as a NetworkX snapshot.

    Built **deterministically** from the settled canonicalization vault rooted
    at the SAME per-workspace storage the concept/observation lists read
    (``<knowledge_vault_root>/<region>/<workspace_id>/``) — see
    :func:`backend.knowledge.canonicalization.concept_graph.build_concept_graph`.
    Active concepts become nodes; concepts that co-occur in the same garden
    observation become weighted ``co-occurs`` edges, and alias/merged links
    between concept nodes become ``alias-of`` edges. No LLM and no network are
    involved.

    Pure + read-only: a vault outside this workspace is not addressable, and a
    fresh workspace yields an empty graph (handled gracefully upstream).

    Overridable in tests via ``app.dependency_overrides``.
    """
    return await build_concept_graph(storage)


__all__ = [
    "build_inside_graph",
    "build_inside_index",
    "build_inside_storage",
]
