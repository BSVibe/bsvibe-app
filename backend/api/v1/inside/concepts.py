"""Concepts endpoints for ``/api/v1/inside`` — list canonical anchors + detail.

Both endpoints read the workspace canonicalization vault via the
:class:`InMemoryCanonicalizationIndex` (FS-as-SoT). The detail endpoint also
walks the deterministic concept graph (the same one :func:`build_concept_graph`
emits) to surface related concepts + origin observations.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

import networkx as nx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.knowledge.canonicalization.concept_graph import concept_ids_in_observation
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)
from backend.knowledge.graph.storage import StorageBackend

from ._dependencies import build_inside_graph, build_inside_index, build_inside_storage
from ._helpers import (
    _DEFAULT_CONCEPT_LIMIT,
    _MAX_CONCEPT_LIMIT,
    _capped_body,
    _excerpt,
)
from ._schemas import (
    ConceptDetailResponse,
    ConceptResponse,
    RelatedConcept,
    SourceObservation,
)

router = APIRouter()


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


@router.get("/concepts/{concept_id}")
async def get_concept_detail(
    concept_id: str,
    index: Annotated[InMemoryCanonicalizationIndex, Depends(build_inside_index)],
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    graph: Annotated[nx.MultiDiGraph, Depends(build_inside_graph)],
) -> ConceptDetailResponse:
    """Inspect one canonical anchor — identity, related concepts, origin/usage.

    Returns the concept's display name + aliases, its **related concepts** (its
    neighbours in :func:`build_concept_graph`, with the co-occurrence weight),
    and its **source observations** — the garden notes whose tags resolve onto
    this concept (title + short excerpt + date), resolved the SAME way the graph
    builder resolves tags → concepts (so the inspector and the graph never
    drift). Strictly read-only.

    A 404 is returned when ``concept_id`` is not an active concept (a tombstone,
    a deprecated id, or an unknown/other-workspace id is simply not on the
    wall) — never a 500 or a misleading empty 200.
    """
    concept = await index.get_active_concept(concept_id)
    if concept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"concept not found: {concept_id}",
        )

    # Related = the concept's graph neighbours. The builder emits a single
    # undirected edge per relationship (stored in one direction), so collapse
    # both successor + predecessor edges and keep the strongest weight seen for
    # each neighbour. Self-loops (defensive) are excluded.
    related_weights: dict[str, float] = {}
    if graph.has_node(concept_id):
        for _src, neighbour, attrs in graph.out_edges(concept_id, data=True):
            if neighbour == concept_id:
                continue
            weight = float(attrs.get("weight", 0.5))
            related_weights[neighbour] = max(related_weights.get(neighbour, 0.0), weight)
        for neighbour, _dst, attrs in graph.in_edges(concept_id, data=True):
            if neighbour == concept_id:
                continue
            weight = float(attrs.get("weight", 0.5))
            related_weights[neighbour] = max(related_weights.get(neighbour, 0.0), weight)

    related = [
        RelatedConcept(
            id=str(neighbour),
            name=str(graph.nodes[neighbour].get("name") or neighbour),
            weight=weight,
        )
        for neighbour, weight in related_weights.items()
    ]
    # Strongest relationship first, then a stable id tiebreaker.
    related.sort(key=lambda r: (-r.weight, r.id))

    # Source observations = the garden notes whose tags resolve onto THIS
    # concept, using the exact resolution the graph builder uses (so the
    # inspector's "origin/usage" matches the co-occurrence edges).
    resolver = TagResolver(index=index)
    store = NoteStore(storage)
    observations: list[tuple[str | None, str, SourceObservation]] = []
    for path in await store.list_garden_paths():
        present = await concept_ids_in_observation(path, store, resolver)
        if concept_id not in present:
            continue
        text = await storage.read(path)
        fm = extract_frontmatter(text)
        captured_at = fm.get("captured_at")
        captured_str = captured_at if isinstance(captured_at, str) else None
        raw_body = body_after_frontmatter(text)
        full_body, truncated = _capped_body(raw_body)
        observations.append(
            (
                captured_str,
                path,
                SourceObservation(
                    id=path,
                    title=extract_title(text) or PurePosixPath(path).stem,
                    excerpt=_excerpt(raw_body),
                    body=full_body,
                    truncated=truncated,
                    captured_at=captured_str,
                ),
            )
        )
    # Newest first: captured_at descending, then path descending (stable).
    observations.sort(key=lambda r: (r[0] or "", r[1]), reverse=True)

    return ConceptDetailResponse(
        id=concept.concept_id,
        name=concept.display,
        aliases=list(concept.aliases),
        related=related,
        observations=[resp for _captured, _path, resp in observations],
    )


__all__ = ["router"]
