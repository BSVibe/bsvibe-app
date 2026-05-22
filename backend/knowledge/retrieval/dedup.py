"""Semantic entity deduplication for the BSage knowledge graph.

Beyond exact name normalization (handled in GraphBackend.upsert_entity),
this module detects semantic duplicates — abbreviations, synonyms,
multilingual variants — using an LLM.

Examples:
    "서울대학교" = "서울대" = "SNU"
    "NYC" = "New York City"
    "Marco's car" = "Marco's vehicle"
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from backend.knowledge.graph.graph_backend import GraphBackend
from backend.knowledge.graph.graph_models import GraphEntity, normalize_name

logger = structlog.get_logger(__name__)

LLMFn = Callable[[str, str], Awaitable[str]]


@dataclass
class DuplicateDecision:
    """LLM's decision about whether two entities are duplicates."""

    is_duplicate: bool
    canonical_id: str | None = None  # The surviving entity ID
    alias_for: str | None = None  # Name to register as alias
    reason: str = ""


_DEDUP_PROMPT = """You are evaluating whether two knowledge graph entities refer to the same thing.

Return JSON only, no prose.

Rules:
- Abbreviations of the same thing are duplicates: "NYC" == "New York City", "SNU" == "서울대학교".
- Synonyms referring to the same entity are duplicates: "Marco's car" == "Marco's vehicle".
- Homonyms are NOT duplicates: "Java (language)" != "Java (island)".
- Different entities with similar names are NOT duplicates: "Python 2" != "Python 3".
- Same proper noun in different languages IS a duplicate: "서울" == "Seoul".

Respond with JSON:
{"is_duplicate": true|false, "reason": "<short explanation>"}
"""


async def llm_check_duplicate(
    llm_fn: LLMFn,
    candidate_a: GraphEntity,
    candidate_b: GraphEntity,
) -> DuplicateDecision:
    """Ask the LLM whether two entities are duplicates.

    Fast path: exact normalized name match returns True without LLM call.
    """
    # Fast path: already-identical normalized names
    if (
        normalize_name(candidate_a.name) == normalize_name(candidate_b.name)
        and candidate_a.entity_type == candidate_b.entity_type
    ):
        return DuplicateDecision(
            is_duplicate=True,
            canonical_id=candidate_a.id,
            alias_for=candidate_b.name,
            reason="exact name match",
        )

    user_msg = json.dumps(
        {
            "entity_a": {"name": candidate_a.name, "type": candidate_a.entity_type},
            "entity_b": {"name": candidate_b.name, "type": candidate_b.entity_type},
        },
        ensure_ascii=False,
    )

    try:
        response = await llm_fn(_DEDUP_PROMPT, user_msg)
        # Strip markdown code fences if present
        response = response.strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[1] if "\n" in response else response
            response = response.rsplit("```", 1)[0].strip()
            if response.startswith("json"):
                response = response[4:].strip()

        data = json.loads(response)
        is_dup = bool(data.get("is_duplicate"))
        return DuplicateDecision(
            is_duplicate=is_dup,
            canonical_id=candidate_a.id if is_dup else None,
            alias_for=candidate_b.name if is_dup else None,
            reason=str(data.get("reason", "")),
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("dedup_llm_parse_failed", error=str(exc))
        return DuplicateDecision(is_duplicate=False, reason="llm parse error")


async def find_semantic_duplicates(
    backend: GraphBackend,
    entity: GraphEntity,
    llm_fn: LLMFn,
    *,
    candidate_limit: int = 5,
) -> list[tuple[GraphEntity, DuplicateDecision]]:
    """Search for semantic duplicates of a given entity.

    Uses substring search to find candidates, then asks the LLM to decide
    whether each is a true duplicate. Returns list of (candidate, decision)
    for all candidates where is_duplicate=True.
    """
    # Use first word or two as search query
    query_words = entity.name.split()[:2]
    query = " ".join(query_words) if query_words else entity.name

    candidates = await backend.search_entities(query, limit=candidate_limit)
    duplicates: list[tuple[GraphEntity, DuplicateDecision]] = []

    for cand in candidates:
        if cand.id == entity.id:
            continue
        if cand.entity_type != entity.entity_type:
            continue
        decision = await llm_check_duplicate(llm_fn, entity, cand)
        if decision.is_duplicate:
            duplicates.append((cand, decision))

    return duplicates


async def merge_duplicate(
    backend: GraphBackend,
    canonical: GraphEntity,
    duplicate: GraphEntity,
) -> int:
    """Merge a duplicate entity into its canonical.

    Moves all relationships pointing to/from the duplicate to the canonical.
    Records the duplicate's name as an alias in canonical.properties.

    Returns the number of relationships migrated.
    """
    migrated = 0
    neighbors = await backend.query_neighbors(duplicate.id)

    for rel, _ent in neighbors:
        # Rewrite relationship to point to canonical
        new_src = canonical.id if rel.source_id == duplicate.id else rel.source_id
        new_tgt = canonical.id if rel.target_id == duplicate.id else rel.target_id
        rel.source_id = new_src
        rel.target_id = new_tgt
        await backend.upsert_relationship(rel)
        migrated += 1

    # Add duplicate's name as alias
    aliases = canonical.properties.get("aliases", [])
    if duplicate.name not in aliases:
        aliases.append(duplicate.name)
    canonical.properties["aliases"] = aliases
    await backend.upsert_entity(canonical)

    # Remove duplicate by source path
    await backend.delete_by_source(duplicate.source_path)

    logger.info(
        "duplicate_merged",
        canonical=canonical.name,
        duplicate=duplicate.name,
        relationships_migrated=migrated,
    )
    return migrated
