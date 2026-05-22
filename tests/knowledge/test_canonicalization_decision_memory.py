"""Tests for DecisionMemory + index decision/policy lookup (Handoff §8)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


@pytest.fixture
async def index(storage: FileSystemStorage) -> InMemoryCanonicalizationIndex:
    idx = InMemoryCanonicalizationIndex()
    await idx.initialize(storage)
    return idx


@pytest.fixture
def memory(index: InMemoryCanonicalizationIndex, store: NoteStore) -> DecisionMemory:
    return DecisionMemory(index=index, store=store)


def _decision(
    kind: str,
    subjects: tuple[str, ...],
    *,
    base_confidence: float = 0.95,
    decay_profile: str = "definitional",
    halflife_days: int | None = None,
    last_confirmed_at: datetime | None = None,
    status: str = "active",
    slug: str | None = None,
) -> models.DecisionEntry:
    last = last_confirmed_at or datetime(2026, 5, 6, 16, 0, 0)
    slug_str = slug or "-".join(subjects)
    return models.DecisionEntry(
        path=f"decisions/{kind}/20260506-160000-{slug_str}.md",
        kind=kind,
        status=status,
        maturity="seedling",
        decision_schema_version=f"{kind}-v1",
        subjects=subjects,
        base_confidence=base_confidence,
        last_confirmed_at=last,
        decay_profile=decay_profile,
        decay_halflife_days=halflife_days,
        valid_from=datetime(2026, 5, 6, 16, 0, 0),
        created_at=datetime(2026, 5, 6, 16, 0, 0),
        updated_at=datetime(2026, 5, 6, 16, 0, 0),
    )


class TestEffectiveStrength:
    def test_definitional_no_decay(self) -> None:
        d = _decision("cannot-link", ("ci", "cd"))
        now = datetime(2030, 1, 1)
        assert DecisionMemory.effective_strength(d, now=now) == 0.95

    def test_semantic_halflife(self) -> None:
        d = _decision(
            "must-link",
            ("auth", "authn"),
            base_confidence=1.0,
            decay_profile="semantic",
        )
        # 365 days = 1 halflife → 0.5
        now = d.last_confirmed_at + timedelta(days=365)
        result = DecisionMemory.effective_strength(d, now=now)
        assert abs(result - 0.5) < 0.01

    def test_explicit_halflife_override(self) -> None:
        d = _decision(
            "cannot-link",
            ("a", "b"),
            base_confidence=0.8,
            decay_profile="semantic",
            halflife_days=10,
        )
        now = d.last_confirmed_at + timedelta(days=20)  # 2 halflives
        result = DecisionMemory.effective_strength(d, now=now)
        assert abs(result - (0.8 * 0.25)) < 0.01

    def test_inactive_decision_returns_zero(self) -> None:
        d = _decision("cannot-link", ("ci", "cd"), status="superseded")
        assert DecisionMemory.effective_strength(d, now=datetime.now()) == 0.0

    def test_expired_decision_returns_zero(self) -> None:
        d = _decision("cannot-link", ("ci", "cd"))
        d.expires_at = datetime(2026, 5, 6, 16, 0, 0)
        # now is after expiry
        now = datetime(2026, 6, 6)
        assert DecisionMemory.effective_strength(d, now=now) == 0.0


class TestFindCannotLink:
    @pytest.mark.asyncio
    async def test_finds_decision_by_subject_pair(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        memory: DecisionMemory,
        storage: FileSystemStorage,
    ) -> None:
        d = _decision("cannot-link", ("ci", "cd"))
        await store.write_decision(d)
        await index.invalidate(d.path)

        # Order-independent: subjects matched as set
        result = await memory.find_cannot_link(("ci", "cd"))
        assert len(result) == 1
        result2 = await memory.find_cannot_link(("cd", "ci"))
        assert len(result2) == 1

    @pytest.mark.asyncio
    async def test_excludes_inactive(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        memory: DecisionMemory,
    ) -> None:
        d = _decision("cannot-link", ("ci", "cd"), status="superseded")
        await store.write_decision(d)
        await index.invalidate(d.path)

        assert await memory.find_cannot_link(("ci", "cd")) == []

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, memory: DecisionMemory) -> None:
        assert await memory.find_cannot_link(("a", "b")) == []

    @pytest.mark.asyncio
    async def test_does_not_match_wrong_kind(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        memory: DecisionMemory,
    ) -> None:
        d = _decision("must-link", ("ci", "cd"))
        await store.write_decision(d)
        await index.invalidate(d.path)
        assert await memory.find_cannot_link(("ci", "cd")) == []
        assert len(await memory.find_must_link(("ci", "cd"))) == 1


class TestMaxEffectiveStrength:
    @pytest.mark.asyncio
    async def test_returns_max_across_matching(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        memory: DecisionMemory,
    ) -> None:
        weak = _decision(
            "cannot-link",
            ("ci", "cd"),
            base_confidence=0.5,
            slug="ci-cd-weak",
        )
        strong = _decision(
            "cannot-link",
            ("ci", "cd"),
            base_confidence=0.9,
            slug="ci-cd-strong",
        )
        await store.write_decision(weak)
        await store.write_decision(strong)
        await index.invalidate(weak.path)
        await index.invalidate(strong.path)

        result = await memory.max_cannot_link_strength(("ci", "cd"), now=datetime(2030, 1, 1))
        assert result == 0.9
