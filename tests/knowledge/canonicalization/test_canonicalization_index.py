"""Tests for CanonicalizationIndex ABC + InMemoryCanonicalizationIndex (Handoff §10)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import (
    CanonicalizationIndex,
    InMemoryCanonicalizationIndex,
)
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
async def index(storage: FileSystemStorage) -> CanonicalizationIndex:
    idx = InMemoryCanonicalizationIndex()
    await idx.initialize(storage)
    return idx


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


async def _seed_concept(
    store: NoteStore,
    concept_id: str,
    title: str,
    aliases: list[str] | None = None,
) -> None:
    await store.write_concept(
        models.ConceptEntry(
            concept_id=concept_id,
            path=f"concepts/active/{concept_id}.md",
            display=title,
            aliases=aliases or [],
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
    )


async def _seed_tombstone(
    storage: FileSystemStorage,
    old_id: str,
    merged_into: str,
) -> None:
    text = f"---\nmerged_into: {merged_into}\nmerged_at: '2026-05-06T14:45:00'\n---\n# {old_id}\n"
    await storage.write(f"concepts/merged/{old_id}.md", text)


async def _seed_deprecated(
    storage: FileSystemStorage,
    concept_id: str,
    replacement: str | None = None,
) -> None:
    text = "---\ndeprecated_at: '2026-05-06T14:45:00'\n"
    if replacement:
        text += f"replacement: {replacement}\n"
    text += "---\n# old\n"
    await storage.write(f"concepts/deprecated/{concept_id}.md", text)


async def _seed_action(
    store: NoteStore,
    path: str,
    kind: str,
    status: str,
    params: dict,
) -> None:
    await store.write_action(
        models.ActionEntry(
            path=path,
            kind=kind,
            status=status,
            action_schema_version=f"{kind}-v1",
            params=params,
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            expires_at=datetime(2026, 5, 7),
        )
    )


class TestRebuildFromVault:
    async def test_empty_vault(self, index: CanonicalizationIndex) -> None:
        assert await index.get_active_concept("anything") is None
        assert await index.find_concepts_by_alias("anything") == []

    async def test_loads_active_concept(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning", ["ML", "ml"])
        await index.rebuild_from_vault(storage)

        entry = await index.get_active_concept("machine-learning")
        assert entry is not None
        assert entry.concept_id == "machine-learning"
        assert entry.display == "Machine Learning"
        assert "ML" in entry.aliases

    async def test_alias_lookup(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning", ["ML", "ml"])
        await index.rebuild_from_vault(storage)

        result = await index.find_concepts_by_alias("ml")
        assert len(result) == 1
        assert result[0].concept_id == "machine-learning"

    async def test_alias_collision_returns_multiple(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning", ["ml"])
        await _seed_concept(store, "meta-learning", "Meta Learning", ["ml"])
        await index.rebuild_from_vault(storage)

        result = await index.find_concepts_by_alias("ml")
        assert len(result) == 2
        assert {c.concept_id for c in result} == {"machine-learning", "meta-learning"}

    async def test_alias_lookup_is_case_insensitive(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning", ["ML"])
        await index.rebuild_from_vault(storage)

        result = await index.find_concepts_by_alias("ml")
        assert len(result) == 1


class TestTombstones:
    async def test_get_tombstone(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "machine-learning", "ML")
        await _seed_tombstone(storage, "ml", "machine-learning")
        await index.rebuild_from_vault(storage)

        ts = await index.get_tombstone("ml")
        assert ts is not None
        assert ts.merged_into == "machine-learning"
        assert ts.old_id == "ml"

    async def test_missing_tombstone(self, index: CanonicalizationIndex) -> None:
        assert await index.get_tombstone("nope") is None


class TestDeprecated:
    async def test_get_deprecated(
        self, index: CanonicalizationIndex, storage: FileSystemStorage
    ) -> None:
        await _seed_deprecated(storage, "old-thing", replacement="new-thing")
        await index.rebuild_from_vault(storage)

        d = await index.get_deprecated("old-thing")
        assert d is not None
        assert d.concept_id == "old-thing"
        assert d.replacement == "new-thing"


class TestPendingDraft:
    async def test_finds_pending_draft_by_concept_param(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_action(
            store,
            "actions/create-concept/x.md",
            "create-concept",
            "draft",
            {"concept": "machine-learning", "title": "ML"},
        )
        await index.rebuild_from_vault(storage)

        result = await index.find_pending_concept_draft("machine-learning")
        assert result is not None
        assert result.kind == "create-concept"
        assert result.params["concept"] == "machine-learning"

    async def test_does_not_return_terminal_draft(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_action(
            store,
            "actions/create-concept/x.md",
            "create-concept",
            "applied",
            {"concept": "machine-learning", "title": "ML"},
        )
        await _seed_action(
            store,
            "actions/create-concept/y.md",
            "create-concept",
            "rejected",
            {"concept": "rejected-thing", "title": "X"},
        )
        await index.rebuild_from_vault(storage)

        # `applied` is terminal — concept is now active, not pending
        assert await index.find_pending_concept_draft("machine-learning") is None
        # `rejected` also terminal
        assert await index.find_pending_concept_draft("rejected-thing") is None

    async def test_pending_approval_counts_as_pending(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_action(
            store,
            "actions/create-concept/x.md",
            "create-concept",
            "pending_approval",
            {"concept": "machine-learning", "title": "ML"},
        )
        await index.rebuild_from_vault(storage)

        result = await index.find_pending_concept_draft("machine-learning")
        assert result is not None


class TestInvalidate:
    async def test_invalidate_concept_removes_entry(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        await _seed_concept(store, "ml", "ML", ["machine_learning"])
        await index.rebuild_from_vault(storage)
        assert await index.get_active_concept("ml") is not None

        await storage.delete("concepts/active/ml.md")
        await index.invalidate("concepts/active/ml.md")

        assert await index.get_active_concept("ml") is None
        assert await index.find_concepts_by_alias("machine_learning") == []

    async def test_invalidate_picks_up_new_concept(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        # Empty index initially
        assert await index.get_active_concept("ml") is None

        await _seed_concept(store, "ml", "ML")
        await index.invalidate("concepts/active/ml.md")

        entry = await index.get_active_concept("ml")
        assert entry is not None

    async def test_invalidate_action_picks_up_status_change(
        self, index: CanonicalizationIndex, storage: FileSystemStorage, store: NoteStore
    ) -> None:
        path = "actions/create-concept/x.md"
        await _seed_action(store, path, "create-concept", "draft", {"concept": "ml", "title": "ML"})
        await index.rebuild_from_vault(storage)
        assert await index.find_pending_concept_draft("ml") is not None

        # Mark as applied
        await _seed_action(
            store,
            path,
            "create-concept",
            "applied",
            {"concept": "ml", "title": "ML"},
        )
        await index.invalidate(path)
        assert await index.find_pending_concept_draft("ml") is None
