"""Contradiction resolution for the BSage knowledge graph.

Detects contradictory facts (same source-target pair with conflicting
rel_type or values) and resolves them using temporal ordering: the newer
fact invalidates the older one by setting ``valid_to``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import GraphRelationship

logger = structlog.get_logger(__name__)


async def detect_contradictions(
    backend: GraphBackend,
    new_rel: GraphRelationship,
) -> list[GraphRelationship]:
    """Find existing relationships that contradict a new one.

    A contradiction is an existing edge between the same source and target
    with the same rel_type but different properties/values, where neither
    has been invalidated (valid_to is None).

    Returns list of contradicted existing relationships.
    """
    neighbors = await backend.query_neighbors(new_rel.source_id, rel_type=new_rel.rel_type)
    contradictions: list[GraphRelationship] = []

    for existing_rel, _neighbor in neighbors:
        # Must be same direction and same target
        if existing_rel.target_id != new_rel.target_id:
            continue
        # Skip if already invalidated
        if existing_rel.valid_to is not None:
            continue
        # Skip if it's the same edge (update, not contradiction)
        if existing_rel.id == new_rel.id:
            continue
        # Same rel_type, same endpoints, both valid → contradiction
        contradictions.append(existing_rel)

    return contradictions


async def resolve_contradiction(
    backend: GraphBackend,
    old_rel: GraphRelationship,
    new_rel: GraphRelationship,
) -> str:
    """Resolve a contradiction by temporally invalidating the older fact.

    Uses valid_from to determine which is older. If the old relationship
    has an earlier valid_from, it gets valid_to set to the new relationship's
    valid_from. If the new relationship is older, it gets invalidated instead.

    Returns the ID of the invalidated relationship.
    """
    now = datetime.now(UTC).isoformat()

    old_start = old_rel.valid_from or old_rel.recorded_at or ""
    new_start = new_rel.valid_from or new_rel.recorded_at or now

    if old_start <= new_start:
        # Old fact is superseded by new fact
        invalidate_at = new_rel.valid_from or now
        await backend.invalidate_relationship(old_rel.id, invalidate_at)
        logger.info(
            "contradiction_resolved",
            invalidated=old_rel.id,
            kept=new_rel.id,
            rel_type=old_rel.rel_type,
            reason="older_fact_superseded",
        )
        return old_rel.id
    else:
        # New fact is actually older — invalidate it
        invalidate_at = old_rel.valid_from or now
        await backend.invalidate_relationship(new_rel.id, invalidate_at)
        logger.info(
            "contradiction_resolved",
            invalidated=new_rel.id,
            kept=old_rel.id,
            rel_type=new_rel.rel_type,
            reason="newer_fact_kept",
        )
        return new_rel.id


async def detect_and_resolve(
    backend: GraphBackend,
    new_rel: GraphRelationship,
) -> list[str]:
    """Detect and resolve all contradictions for a new relationship.

    Returns list of invalidated relationship IDs.
    """
    contradictions = await detect_contradictions(backend, new_rel)
    invalidated: list[str] = []

    for old_rel in contradictions:
        rid = await resolve_contradiction(backend, old_rel, new_rel)
        invalidated.append(rid)

    return invalidated
