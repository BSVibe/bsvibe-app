"""DecisionMemory — slice 4 decision-strength queries (Handoff §8.1).

Reuses the existing ``bsage.garden.confidence`` decay model
(``decay_factor(days, halflife)``). Per spec §0.1 the decision notes are
SoT; this module is a derived query helper over the index.

Default profile halflives (Handoff §8.1):
- definitional: no decay (effective = base_confidence)
- semantic: 365 days
- episodic: 30 days
- procedural: 90 days
- affective: 60 days

``cannot-link`` decisions default to ``definitional`` per §8.1.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.retrieval.confidence import DecayConfig, decay_factor

_PROFILE_DEFAULT_HALFLIFE: dict[str, int | None] = {
    "definitional": None,  # no decay
    "semantic": 365,
    "episodic": 30,
    "procedural": 90,
    "affective": 60,
}


class DecisionMemory:
    """Query helper over decision notes (Handoff §8.1)."""

    def __init__(
        self,
        index: CanonicalizationIndex,
        store: NoteStore,
        decay_config: DecayConfig | None = None,
    ) -> None:
        self._index = index
        self._store = store
        self._decay_config = decay_config or DecayConfig()

    @staticmethod
    def effective_strength(decision: models.DecisionEntry, *, now: datetime | None = None) -> float:
        """Compute effective strength with status / expiry / decay (Handoff §8.1)."""
        if decision.status != "active":
            return 0.0
        now_dt = _aware(now or datetime.now(tz=UTC))
        if decision.expires_at is not None and now_dt >= _aware(decision.expires_at):
            return 0.0
        # Definitional → no decay
        if decision.decay_profile == "definitional":
            return decision.base_confidence
        halflife = decision.decay_halflife_days
        if halflife is None:
            halflife = _PROFILE_DEFAULT_HALFLIFE.get(decision.decay_profile)
        if not halflife or halflife <= 0:
            return decision.base_confidence
        confirmed = _aware(decision.last_confirmed_at)
        days = (_aware(now_dt) - confirmed).total_seconds() / 86400.0
        return decision.base_confidence * decay_factor(days, halflife)

    async def find_cannot_link(self, subjects: tuple[str, ...]) -> list[models.DecisionEntry]:
        return await self._find(kind="cannot-link", subjects=subjects)

    async def find_must_link(self, subjects: tuple[str, ...]) -> list[models.DecisionEntry]:
        return await self._find(kind="must-link", subjects=subjects)

    async def max_cannot_link_strength(
        self,
        subjects: tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> float:
        decisions = await self.find_cannot_link(subjects)
        if not decisions:
            return 0.0
        return max(self.effective_strength(d, now=now) for d in decisions)

    async def list_active_cannot_link(self) -> list[models.DecisionEntry]:
        return await self._index.list_decisions(kind="cannot-link", status="active")

    async def list_active_must_link(self) -> list[models.DecisionEntry]:
        return await self._index.list_decisions(kind="must-link", status="active")

    # ---------------------------------------------------------------- helpers

    async def _find(self, *, kind: str, subjects: tuple[str, ...]) -> list[models.DecisionEntry]:
        target = frozenset(subjects)
        active = await self._index.list_decisions(kind=kind, status="active")
        return [d for d in active if frozenset(d.subjects) == target]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
