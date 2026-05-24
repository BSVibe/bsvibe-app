"""/api/v1/inside — the founder's read-only window into the knowledge graph.

The trust ratchet (Workflow §5) accumulates knowledge as the AI verifies work:
the SettleWorker deposits raw *garden observations*, and the canonicalization
promoter graduates recurring patterns into *canonical anchors* — the settled
``concepts/active/<id>.md`` "wall". The PWA "Inside" moment is the founder
peeking at what the AI has learned; this router is the backend surface that
moment reads from. It exposes two calm, read-only lists:

* ``GET /inside/concepts`` — the canonical anchors (settled concepts), sourced
  through :meth:`InMemoryCanonicalizationIndex.list_active_concepts` (the SAME
  vault-derived enumeration the canonicalization queue uses, FS-as-SoT).
* ``GET /inside/observations`` — the recent garden observation notes the
  SettleWorker writes (the unpromoted raw samples), newest first.

Workspace isolation is structural — exactly the boundary
:mod:`backend.api.v1.decisions` enforces. Each request builds an index +
storage rooted at ``<knowledge_vault_root>/<region>/<workspace_id>/`` (via the
shared :func:`backend.api.v1.decisions._vault_root` helper), so another
workspace's vault is simply not there: a concept or observation outside the
caller's workspace is never enumerated. No DB table is involved (the vault is
the source of truth) and there is no write path here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

import networkx as nx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from backend.api.deps import get_workspace_id
from backend.api.v1.decisions import _vault_root
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)
from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend
from backend.knowledge.graph.vault_backend import VaultBackend

router = APIRouter()

# Conservative caps — the Inside surface is a calm snapshot, not a data dump.
_DEFAULT_CONCEPT_LIMIT = 50
_MAX_CONCEPT_LIMIT = 200
_DEFAULT_OBSERVATION_LIMIT = 25
_MAX_OBSERVATION_LIMIT = 100

# Force-directed view cap — a calm picture, not the whole graph. When the
# workspace graph exceeds this, keep the most-connected nodes (top-N by
# PageRank centrality) so the founder sees the structurally important hubs.
_MAX_GRAPH_NODES = 200

# Excerpt cap — a short, founder-legible blurb, not the full note body.
_EXCERPT_CHARS = 200


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

    Reads from a :class:`~backend.knowledge.graph.vault_backend.VaultBackend`
    rooted at the SAME per-workspace vault storage the concept/observation lists
    read (``<knowledge_vault_root>/<region>/<workspace_id>/``). The VaultBackend
    loads the ``.bsage/graph_cache.json`` snapshot the GraphSubscriber persists
    from vault writes (FS-as-SoT) — so this is a pure, read-only view of exactly
    the graph the trust ratchet built for THIS workspace. A vault outside it is
    not addressable; a fresh workspace simply has no cache and yields an empty
    graph (handled gracefully upstream).

    Overridable in tests via ``app.dependency_overrides``.
    """
    backend = VaultBackend(storage)
    await backend.initialize()
    return backend.to_networkx()


class ConceptResponse(BaseModel):
    """One canonical anchor (settled concept) on the founder's "wall".

    ``id`` is the concept's vault stem (``concepts/active/<id>.md``); ``name``
    is its display title (the note's H1). ``alias_count`` is the cheap
    connectedness signal available without a graph traversal — how many variant
    spellings resolve onto this anchor. ``summary`` is a short excerpt of the
    concept body (empty for a freshly-promoted anchor that carries only its
    title).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    summary: str
    aliases: list[str]
    alias_count: int
    created_at: datetime
    updated_at: datetime


class GraphNode(BaseModel):
    """One node in the force-directed knowledge graph.

    ``id`` is the entity's graph id (stable across edges); ``label`` its
    human-readable name; ``kind`` its ontology entity type (concept, person,
    project, tool, …); ``weight`` its connectedness signal (degree) so the viz
    can size hubs.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: str | None = None
    weight: int = 0


class GraphEdge(BaseModel):
    """One edge in the force-directed knowledge graph.

    ``source``/``target`` are :class:`GraphNode` ids; ``type`` the ontology
    relationship type; ``weight`` the edge importance (from the relationship's
    extracted weight).
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    type: str | None = None
    weight: float = 0.5


class GraphResponse(BaseModel):
    """The workspace knowledge graph as nodes + edges for a force-directed view.

    An empty/sparse workspace returns ``{nodes: [], edges: []}`` — never an
    error. Edges only ever reference nodes present in ``nodes`` (so a capped
    response stays internally consistent for the renderer).
    """

    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ObservationResponse(BaseModel):
    """One recent garden observation (an unpromoted settle note).

    These are the raw, monotonically-accumulating samples the SettleWorker
    deposits per verified work step (the *learning* half of the ratchet).
    ``id`` is the note's vault path; ``title`` its H1; ``excerpt`` a short blurb
    of the body; ``tags`` the note's content + structural tags; ``captured_at``
    the deposit date the writer stamped.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    excerpt: str
    tags: list[str]
    captured_at: str | None = None


def _excerpt(body: str) -> str:
    """First non-empty body line (after the H1), truncated for a calm blurb."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:_EXCERPT_CHARS]
    return ""


@router.get("/concepts")
async def list_concepts(
    index: Annotated[InMemoryCanonicalizationIndex, Depends(build_inside_index)],
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    limit: Annotated[int, Query(ge=1, le=_MAX_CONCEPT_LIMIT)] = _DEFAULT_CONCEPT_LIMIT,
) -> list[ConceptResponse]:
    """List the workspace's canonical anchors (active concepts), newest first.

    Sourced through :meth:`InMemoryCanonicalizationIndex.list_active_concepts`
    — the existing vault-derived enumeration, NOT a new engine method. Sorted
    by ``updated_at`` so the most recently-settled anchors lead. The concept
    body (if any) is read to build a short excerpt; a freshly-promoted anchor
    carries only its title and yields an empty summary.
    """
    concepts = await index.list_active_concepts()
    concepts.sort(key=lambda c: c.updated_at, reverse=True)
    out: list[ConceptResponse] = []
    for concept in concepts[:limit]:
        summary = ""
        if await storage.exists(concept.path):
            text = await storage.read(concept.path)
            summary = _excerpt(body_after_frontmatter(text))
        out.append(
            ConceptResponse(
                id=concept.concept_id,
                name=concept.display,
                summary=summary,
                aliases=list(concept.aliases),
                alias_count=len(concept.aliases),
                created_at=concept.created_at,
                updated_at=concept.updated_at,
            )
        )
    return out


@router.get("/observations")
async def list_observations(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    limit: Annotated[int, Query(ge=1, le=_MAX_OBSERVATION_LIMIT)] = _DEFAULT_OBSERVATION_LIMIT,
) -> list[ObservationResponse]:
    """List recent garden observation notes (raw settle notes), newest first.

    Garden notes live under ``garden/<maturity>/<slug>.md`` (the SettleWorker
    writes ``garden/seedling/...`` via the GardenWriter). Read straight off the
    vault storage — the same FS-as-SoT store the canonicalization index reads —
    and sorted by the writer-stamped ``captured_at`` (path as a stable
    tiebreaker), so the freshest observations lead.
    """
    paths = await storage.list_files("garden", "*.md")
    rows: list[tuple[str, str | None, ObservationResponse]] = []
    for path in paths:
        text = await storage.read(path)
        fm = extract_frontmatter(text)
        captured_at = fm.get("captured_at")
        captured_str = captured_at if isinstance(captured_at, str) else None
        rows.append(
            (
                path,
                captured_str,
                ObservationResponse(
                    id=path,
                    title=extract_title(text) or PurePosixPath(path).stem,
                    excerpt=_excerpt(body_after_frontmatter(text)),
                    tags=[str(t) for t in (fm.get("tags") or [])],
                    captured_at=captured_str,
                ),
            )
        )
    # Newest first: captured_at descending, then path descending as a stable
    # tiebreaker (notes without a date sort last).
    rows.sort(key=lambda r: (r[1] or "", r[0]), reverse=True)
    return [resp for _, _, resp in rows[:limit]]


@router.get("/graph")
async def get_graph(
    graph: Annotated[nx.MultiDiGraph, Depends(build_inside_graph)],
) -> GraphResponse:
    """The workspace knowledge graph as nodes + edges for a force-directed view.

    Entities → nodes (id + display name + entity_type + degree), relationships →
    edges (source/target/type/weight), sourced from the per-workspace
    ``VaultBackend`` snapshot (FS-as-SoT). Strictly read-only.

    When the graph is large it is capped to the ``_MAX_GRAPH_NODES`` most-
    connected nodes (top-N by degree — the hubs the founder cares about);
    edges are filtered to those between surviving nodes so the response stays
    internally consistent. A fresh/sparse workspace yields
    ``{nodes: [], edges: []}`` — 200, never an error.
    """
    if graph.number_of_nodes() == 0:
        return GraphResponse(nodes=[], edges=[])

    degrees = dict(graph.degree())

    # Cap to the most-connected nodes (highest degree — the structural hubs)
    # when the graph exceeds the view budget. Degree, not PageRank: a hub with
    # many out-edges should survive, but PageRank flows rank *to* its leaves.
    if graph.number_of_nodes() > _MAX_GRAPH_NODES:
        ranked = sorted(degrees.items(), key=lambda item: item[1], reverse=True)
        keep_ids = {node_id for node_id, _deg in ranked[:_MAX_GRAPH_NODES]}
    else:
        keep_ids = set(graph.nodes())

    nodes = [
        GraphNode(
            id=str(node_id),
            label=str(attrs.get("name") or node_id),
            kind=(str(attrs["entity_type"]) if attrs.get("entity_type") else None),
            weight=int(degrees.get(node_id, 0)),
        )
        for node_id, attrs in graph.nodes(data=True)
        if node_id in keep_ids
    ]

    # Dedupe parallel multi-edges into a single edge per (source, target, type)
    # — a calm picture, not every recorded fact — and keep only edges between
    # surviving (kept) nodes.
    seen: set[tuple[str, str, str | None]] = set()
    edges: list[GraphEdge] = []
    for source, target, attrs in graph.edges(data=True):
        if source not in keep_ids or target not in keep_ids:
            continue
        rel_type = str(attrs["rel_type"]) if attrs.get("rel_type") else None
        key = (str(source), str(target), rel_type)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            GraphEdge(
                source=str(source),
                target=str(target),
                type=rel_type,
                weight=float(attrs.get("weight", 0.5)),
            )
        )

    return GraphResponse(nodes=nodes, edges=edges)
