"""Tests for TagResolver — Handoff §11 resolution algorithm."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


@pytest.fixture
async def resolver(storage: FileSystemStorage) -> TagResolver:
    idx = InMemoryCanonicalizationIndex()
    await idx.initialize(storage)
    return TagResolver(index=idx)


async def _seed_concept(
    store: NoteStore,
    concept_id: str,
    title: str = "Title",
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


async def _seed_tombstone(storage: FileSystemStorage, old_id: str, merged_into: str) -> None:
    text = f"---\nmerged_into: {merged_into}\nmerged_at: '2026-05-06T14:45:00'\n---\n# {old_id}\n"
    await storage.write(f"concepts/merged/{old_id}.md", text)


async def _seed_deprecated(
    storage: FileSystemStorage, concept_id: str, replacement: str | None = None
) -> None:
    text = "---\ndeprecated_at: '2026-05-06T14:45:00'\n"
    if replacement:
        text += f"replacement: {replacement}\n"
    text += "---\n# old\n"
    await storage.write(f"concepts/deprecated/{concept_id}.md", text)


class TestNormalize:
    def test_lowercase(self) -> None:
        assert TagResolver.normalize("Machine Learning") == "machine-learning"

    def test_underscores_to_hyphens(self) -> None:
        assert TagResolver.normalize("machine_learning") == "machine-learning"

    def test_spaces_to_hyphens(self) -> None:
        assert TagResolver.normalize("machine learning") == "machine-learning"

    def test_collapses_repeated_separators(self) -> None:
        assert TagResolver.normalize("self--hosting") == "self-hosting"
        assert TagResolver.normalize("self  hosting") == "self-hosting"
        assert TagResolver.normalize("self_-_hosting") == "self-hosting"

    def test_strips_edges(self) -> None:
        assert TagResolver.normalize("  ml  ") == "ml"
        assert TagResolver.normalize("-ml-") == "ml"

    def test_drops_invalid_chars(self) -> None:
        assert TagResolver.normalize("c++") == "c"
        assert TagResolver.normalize("foo.bar") == "foo-bar"

    def test_pure_garbage_returns_empty(self) -> None:
        assert TagResolver.normalize("...") == ""
        assert TagResolver.normalize("") == ""


class TestResolveDirectActive:
    async def test_exact_concept_id_match(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("machine-learning")
        assert result.status == "resolved"
        assert result.concept_id == "machine-learning"

    async def test_normalization_then_active_match(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "machine-learning", "Machine Learning")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("Machine Learning")
        assert result.status == "resolved"
        assert result.concept_id == "machine-learning"

    async def test_exact_alias_match(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "machine-learning", "ML", aliases=["ML"])
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("ML")
        assert result.status == "resolved"
        assert result.concept_id == "machine-learning"


class TestResolveTombstone:
    async def test_redirect_to_active(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "machine-learning", "ML")
        await _seed_tombstone(storage, "ml", "machine-learning")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("ml")
        assert result.status == "resolved"
        assert result.concept_id == "machine-learning"
        assert result.redirected_from == "ml"

    async def test_chained_redirect_collapses(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        # Spec §3.2: tombstones MUST NOT chain. But resolver is robust:
        # walking with visited set, must terminate at active.
        await _seed_concept(store, "machine-learning", "ML")
        await _seed_tombstone(storage, "ml", "machine-learning")
        await _seed_tombstone(storage, "m-l", "ml")  # malformed but resolver follows
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("m-l")
        assert result.status == "resolved"
        assert result.concept_id == "machine-learning"

    async def test_redirect_cycle_returns_blocked(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_tombstone(storage, "a", "b")
        await _seed_tombstone(storage, "b", "a")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("a")
        assert result.status == "blocked"

    async def test_redirect_to_missing_returns_blocked(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_tombstone(storage, "ml", "nonexistent")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("ml")
        assert result.status == "blocked"


class TestResolveDeprecated:
    async def test_deprecated_returns_blocked_with_suggestion(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "new-thing", "New Thing")
        await _seed_deprecated(storage, "old-thing", replacement="new-thing")
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("old-thing")
        assert result.status == "blocked"
        assert result.deprecated_replacement == "new-thing"


class TestResolveAmbiguous:
    async def test_alias_collision_returns_ambiguous(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await _seed_concept(store, "machine-learning", "ML", aliases=["ml-thing"])
        await _seed_concept(store, "meta-learning", "Meta", aliases=["ml-thing"])
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("ml-thing")
        assert result.status == "ambiguous"
        assert set(result.ambiguous_candidates) == {"machine-learning", "meta-learning"}

    async def test_active_id_takes_precedence_over_collision(
        self, resolver: TagResolver, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        # If "ml" is BOTH an active concept id AND an alias of another concept,
        # exact id match wins (Handoff §11 step 3 before step 4).
        await _seed_concept(store, "ml", "ML Direct")
        await _seed_concept(store, "machine-learning", "ML", aliases=["ml"])
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("ml")
        assert result.status == "resolved"
        assert result.concept_id == "ml"


class TestResolvePendingCandidate:
    async def test_pending_draft_returns_pending_candidate(
        self,
        resolver: TagResolver,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        await store.write_action(
            models.ActionEntry(
                path="actions/create-concept/x.md",
                kind="create-concept",
                status="draft",
                action_schema_version="create-concept-v1",
                params={"concept": "machine-learning", "title": "ML"},
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
                expires_at=datetime(2026, 5, 7),
            )
        )
        await resolver._index.rebuild_from_vault(storage)

        result = await resolver.resolve("machine-learning")
        assert result.status == "pending_candidate"
        assert result.pending_draft == "actions/create-concept/x.md"


class TestResolveNewCandidate:
    async def test_no_match_returns_new_candidate(self, resolver: TagResolver) -> None:
        result = await resolver.resolve("totally-new-thing")
        assert result.status == "new_candidate"
        assert result.concept_id == "totally-new-thing"  # the normalized form

    async def test_normalization_carries_through(self, resolver: TagResolver) -> None:
        result = await resolver.resolve("Totally New Thing")
        assert result.status == "new_candidate"
        assert result.concept_id == "totally-new-thing"


class TestResolveBlocked:
    async def test_empty_normalization_returns_blocked(self, resolver: TagResolver) -> None:
        result = await resolver.resolve("...")
        assert result.status == "blocked"
        assert result.concept_id is None

    async def test_invalid_id_after_normalize_returns_blocked(self, resolver: TagResolver) -> None:
        # normalize("123") starts with a digit — invalid concept-id regex
        result = await resolver.resolve("123")
        assert result.status == "blocked"
