"""Note maturity lifecycle — status constants and promotion evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.skill_context import GraphInterface
    GraphInterface = Any

logger = structlog.get_logger(__name__)


class NoteMaturity(StrEnum):
    """Garden note maturity stages, ordered from youngest to most mature."""

    SEED = "seed"
    SEEDLING = "seedling"
    BUDDING = "budding"
    EVERGREEN = "evergreen"


MATURITY_ORDER: list[NoteMaturity] = [
    NoteMaturity.SEED,
    NoteMaturity.SEEDLING,
    NoteMaturity.BUDDING,
    NoteMaturity.EVERGREEN,
]


def normalize_status(status: str) -> NoteMaturity:
    """Normalize a status string to a NoteMaturity value.

    Returns ``SEED`` for unknown statuses.
    """
    try:
        return NoteMaturity(status)
    except ValueError:
        return NoteMaturity.SEED


@dataclass
class MaturityConfig:
    """Configurable thresholds for maturity promotion."""

    seedling_min_relationships: int = 2
    budding_min_sources: int = 3
    evergreen_min_days_stable: int = 14
    evergreen_min_relationships: int = 5


class MaturityEvaluator:
    """Evaluates garden notes for maturity promotion *and* demotion based on graph metrics."""

    def __init__(self, graph: GraphInterface, config: MaturityConfig) -> None:
        self._graph = graph
        self._config = config

    async def evaluate(self, note_path: str, current_status: str) -> NoteMaturity | None:
        """Evaluate whether a note should be promoted or demoted.

        Args:
            note_path: Relative vault path of the note.
            current_status: Current status string from the note's frontmatter.

        Returns:
            The new maturity level if change is warranted, or ``None``.
        """
        status = normalize_status(current_status)

        # Check demotion first (higher statuses can lose support)
        demotion = await self._check_demotion(note_path, status)
        if demotion is not None:
            return demotion

        # Then check promotion
        idx = MATURITY_ORDER.index(status)
        if idx >= len(MATURITY_ORDER) - 1:
            return None  # already evergreen

        if status == NoteMaturity.SEED:
            return await self._check_seed_to_seedling(note_path)
        if status == NoteMaturity.SEEDLING:
            return await self._check_seedling_to_budding(note_path)
        if status == NoteMaturity.BUDDING:
            return await self._check_budding_to_evergreen(note_path)

        return None

    async def _check_demotion(self, note_path: str, current: NoteMaturity) -> NoteMaturity | None:
        """Check if a note should be demoted due to lost graph support.

        Demotion rules:
        - EVERGREEN → BUDDING: relationships drop below evergreen threshold
        - BUDDING → SEEDLING: distinct sources drop below budding threshold
        - SEEDLING → SEED: relationships drop below seedling threshold
        - SEED: cannot demote further
        """
        if current == NoteMaturity.SEED:
            return None

        rel_count = await self._graph.count_relationships_for_entity(note_path)

        if (
            current == NoteMaturity.EVERGREEN
            and rel_count < self._config.evergreen_min_relationships
        ):
            return NoteMaturity.BUDDING

        if current in (NoteMaturity.BUDDING, NoteMaturity.EVERGREEN):
            source_count = await self._graph.count_distinct_sources(note_path)
            if current == NoteMaturity.BUDDING and source_count < self._config.budding_min_sources:
                return NoteMaturity.SEEDLING

        if current == NoteMaturity.SEEDLING and rel_count < self._config.seedling_min_relationships:
            return NoteMaturity.SEED

        return None

    async def _check_seed_to_seedling(self, note_path: str) -> NoteMaturity | None:
        rel_count = await self._graph.count_relationships_for_entity(note_path)
        if rel_count >= self._config.seedling_min_relationships:
            return NoteMaturity.SEEDLING
        return None

    async def _check_seedling_to_budding(self, note_path: str) -> NoteMaturity | None:
        source_count = await self._graph.count_distinct_sources(note_path)
        if source_count >= self._config.budding_min_sources:
            return NoteMaturity.BUDDING
        return None

    async def _check_budding_to_evergreen(self, note_path: str) -> NoteMaturity | None:
        updated_at = await self._graph.get_entity_updated_at(note_path)
        if updated_at is None:
            return None

        try:
            last_update = datetime.fromisoformat(updated_at).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

        days_stable = (datetime.now(tz=UTC) - last_update).days
        if days_stable < self._config.evergreen_min_days_stable:
            return None

        rel_count = await self._graph.count_relationships_for_entity(note_path)
        if rel_count >= self._config.evergreen_min_relationships:
            return NoteMaturity.EVERGREEN
        return None
