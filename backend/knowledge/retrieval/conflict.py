"""Knowledge conflict resolution — detects and resolves contradictory facts."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)

# Source type priority: explicit > inferred > observed
_SOURCE_TYPE_PRIORITY = {"explicit": 3, "inferred": 2, "observed": 1}


@dataclass
class FactRecord:
    """A fact extracted from a vault note for conflict analysis."""

    note_path: str
    subject: str
    predicate: str
    object_: str
    context: str = ""
    source_type: str = "inferred"
    captured_at: str = ""
    confidence: float = 0.9


@dataclass
class ConflictResult:
    """Result of a conflict resolution."""

    winner: FactRecord
    loser: FactRecord
    resolution: str  # "context_scoped" | "source_type" | "recency" | "unresolved"
    explanation: str = ""


def resolve_conflict(fact_a: FactRecord, fact_b: FactRecord) -> ConflictResult:
    """Resolve a conflict between two facts about the same subject+predicate.

    Resolution cascade (v2.2 spec):
    1. Context Scoping — if contexts differ, both are valid (not a conflict)
    2. Source Type priority — explicit > inferred > observed
    3. Recency — newer captured_at wins

    Args:
        fact_a: First fact.
        fact_b: Second fact.

    Returns:
        ConflictResult with winner, loser, and resolution method.
    """
    # Step 1: Context Scoping — different contexts mean no real conflict
    if fact_a.context and fact_b.context and fact_a.context != fact_b.context:
        return ConflictResult(
            winner=fact_a,
            loser=fact_b,
            resolution="context_scoped",
            explanation=(
                f"No conflict: '{fact_a.context}' vs '{fact_b.context}' are different contexts."
            ),
        )

    # Step 2: Source Type priority
    pri_a = _SOURCE_TYPE_PRIORITY.get(fact_a.source_type, 0)
    pri_b = _SOURCE_TYPE_PRIORITY.get(fact_b.source_type, 0)
    if pri_a != pri_b:
        winner, loser = (fact_a, fact_b) if pri_a > pri_b else (fact_b, fact_a)
        return ConflictResult(
            winner=winner,
            loser=loser,
            resolution="source_type",
            explanation=(
                f"'{winner.source_type}' (priority {max(pri_a, pri_b)}) "
                f"overrides '{loser.source_type}' (priority {min(pri_a, pri_b)})."
            ),
        )

    # Step 3: Recency — newer captured_at wins
    if fact_a.captured_at and fact_b.captured_at:
        if fact_a.captured_at > fact_b.captured_at:
            return ConflictResult(
                winner=fact_a,
                loser=fact_b,
                resolution="recency",
                explanation=f"Newer fact ({fact_a.captured_at}) wins over ({fact_b.captured_at}).",
            )
        if fact_b.captured_at > fact_a.captured_at:
            return ConflictResult(
                winner=fact_b,
                loser=fact_a,
                resolution="recency",
                explanation=f"Newer fact ({fact_b.captured_at}) wins over ({fact_a.captured_at}).",
            )

    # Step 4: Unresolved — needs natural confirmation
    return ConflictResult(
        winner=fact_a,
        loser=fact_b,
        resolution="unresolved",
        explanation="Same source_type and recency; needs user confirmation.",
    )


def detect_conflicts(facts: list[FactRecord]) -> list[tuple[FactRecord, FactRecord]]:
    """Find pairs of facts that conflict (same subject+predicate, different object).

    Args:
        facts: List of fact records to analyze.

    Returns:
        List of (fact_a, fact_b) tuples that may conflict.
    """
    by_key: dict[tuple[str, str], list[FactRecord]] = {}
    for fact in facts:
        key = (fact.subject.lower(), fact.predicate.lower())
        by_key.setdefault(key, []).append(fact)

    conflicts: list[tuple[FactRecord, FactRecord]] = []
    for _key, group in by_key.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if group[i].object_.lower() != group[j].object_.lower():
                    conflicts.append((group[i], group[j]))
    return conflicts
