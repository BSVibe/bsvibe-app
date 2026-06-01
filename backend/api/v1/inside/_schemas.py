"""Shared Pydantic schemas for the ``/api/v1/inside`` surface (Lift M1).

Used across the three read endpoints (concepts list/detail, observations,
graph) — extracted here so each endpoint module stays a thin adapter (D35).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    project, tool, …) — the TYPE legend colours by this; ``community`` the
    deterministic emergent-cluster id (:func:`build_concept_graph` assigns one
    per node) the COMMUNITY legend colours by; ``weight`` its connectedness
    signal (degree) so the viz can size hubs.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: str | None = None
    community: str | None = None
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


class RelatedConcept(BaseModel):
    """One neighbour of an inspected concept in the workspace concept graph.

    ``id``/``name`` identify the related anchor (clickable to pivot the
    inspector onto it); ``weight`` is the co-occurrence weight from
    :func:`build_concept_graph` — how strongly the two concepts are related
    (number of shared observations for a ``co-occurs`` edge, ``1.0`` for an
    ``alias-of`` link).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    weight: float


class SourceObservation(BaseModel):
    """One garden observation that references the inspected concept.

    These are the raw settle notes whose tags resolve onto this concept (the
    *origin / usage* of the anchor) — derived with the exact tag→concept
    resolution the graph builder uses. ``id`` is the note's vault path;
    ``title`` its H1; ``excerpt`` a short body blurb; ``body`` the full note
    body (capped, with ``truncated`` set when it overflowed) so the inspector
    can render the note in full; ``captured_at`` the writer-stamped deposit
    date (may be absent).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    excerpt: str
    body: str
    truncated: bool
    captured_at: str | None = None


class ConceptDetailResponse(BaseModel):
    """The read-only inspector behind a clicked concept.

    Identity (``id`` / ``name`` / ``aliases``) plus the two connectedness
    signals the founder cares about: ``related`` (the concept's neighbours in
    the deterministic concept graph, with weight) and ``observations`` (the
    garden notes that reference it — its origin/usage). Strictly read-only:
    Stitch's Edit/Retract affordances map to canonicalization deprecate/edit
    actions that have no v1 endpoint yet and are intentionally not surfaced
    here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    aliases: list[str]
    related: list[RelatedConcept]
    observations: list[SourceObservation]


__all__ = [
    "ConceptDetailResponse",
    "ConceptResponse",
    "GraphEdge",
    "GraphNode",
    "GraphResponse",
    "ObservationResponse",
    "RelatedConcept",
    "SourceObservation",
]
