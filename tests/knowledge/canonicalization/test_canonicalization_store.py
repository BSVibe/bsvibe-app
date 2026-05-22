"""Tests for NoteStore — typed wrapper over StorageBackend (Class_Diagram §5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter, extract_title
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def store(tmp_path: Path) -> NoteStore:
    return NoteStore(FileSystemStorage(tmp_path))


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


class TestReadWriteConcept:
    @pytest.mark.asyncio
    async def test_write_then_read_minimal(self, store: NoteStore) -> None:
        entry = models.ConceptEntry(
            concept_id="machine-learning",
            path="concepts/active/machine-learning.md",
            display="Machine Learning",
            aliases=[],
            created_at=datetime(2026, 5, 6, 14, 30, 12),
            updated_at=datetime(2026, 5, 6, 14, 30, 12),
        )
        await store.write_concept(entry)

        got = await store.read_concept("machine-learning")
        assert got is not None
        assert got.concept_id == "machine-learning"
        assert got.display == "Machine Learning"
        assert got.aliases == []
        assert got.created_at == datetime(2026, 5, 6, 14, 30, 12)

    @pytest.mark.asyncio
    async def test_write_with_aliases_and_source_action(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        entry = models.ConceptEntry(
            concept_id="ml",
            path="concepts/active/ml.md",
            display="Machine Learning",
            aliases=["machine_learning", "ML"],
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            source_action="actions/create-concept/20260506-143012-ml.md",
        )
        await store.write_concept(entry)

        # Verify frontmatter shape on disk (Handoff §3.1 forbidden fields)
        raw = await storage.read("concepts/active/ml.md")
        fm = extract_frontmatter(raw)
        assert fm.get("aliases") == ["machine_learning", "ML"]
        assert fm.get("source_action") == "actions/create-concept/20260506-143012-ml.md"
        # Forbidden fields MUST NOT appear (Handoff §3.1)
        assert "concept_id" not in fm
        assert "canonical_tag" not in fm
        assert "display" not in fm
        assert "status" not in fm
        # H1 carries display label, not frontmatter
        assert extract_title(raw) == "Machine Learning"

    @pytest.mark.asyncio
    async def test_read_missing_returns_none(self, store: NoteStore) -> None:
        assert await store.read_concept("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_concept_exists(self, store: NoteStore) -> None:
        assert not await store.concept_exists("machine-learning")
        await store.write_concept(
            models.ConceptEntry(
                concept_id="machine-learning",
                path="concepts/active/machine-learning.md",
                display="Machine Learning",
                aliases=[],
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
            )
        )
        assert await store.concept_exists("machine-learning")

    @pytest.mark.asyncio
    async def test_write_with_initial_body_preserves_body(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        entry = models.ConceptEntry(
            concept_id="ml",
            path="concepts/active/ml.md",
            display="Machine Learning",
            aliases=[],
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        body = "Some intro paragraph about ML.\n\nMore detail."
        await store.write_concept(entry, initial_body=body)

        raw = await storage.read("concepts/active/ml.md")
        assert "Some intro paragraph about ML." in raw
        assert "More detail." in raw
        assert extract_title(raw) == "Machine Learning"


class TestReadWriteAction:
    @pytest.mark.asyncio
    async def test_write_then_read_round_trip(self, store: NoteStore) -> None:
        entry = models.ActionEntry(
            path="actions/create-concept/20260506-143012-ml.md",
            kind="create-concept",
            status="draft",
            action_schema_version="create-concept-v1",
            params={"concept": "ml", "title": "Machine Learning"},
            created_at=datetime(2026, 5, 6, 14, 30, 12),
            updated_at=datetime(2026, 5, 6, 14, 30, 12),
            expires_at=datetime(2026, 5, 7, 14, 30, 12),
        )
        await store.write_action(entry)

        got = await store.read_action("actions/create-concept/20260506-143012-ml.md")
        assert got is not None
        assert got.kind == "create-concept"
        assert got.status == "draft"
        assert got.params == {"concept": "ml", "title": "Machine Learning"}
        assert got.expires_at == datetime(2026, 5, 7, 14, 30, 12)
        assert got.affected_paths == []

    @pytest.mark.asyncio
    async def test_kind_derived_from_path(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        # Per Handoff §0.2 — action kind is path-derived, NOT in frontmatter
        entry = models.ActionEntry(
            path="actions/retag-notes/20260506-143055-foo.md",
            kind="retag-notes",
            status="draft",
            action_schema_version="retag-notes-v1",
            params={"changes": []},
            created_at=datetime(2026, 5, 6, 14, 30, 55),
            updated_at=datetime(2026, 5, 6, 14, 30, 55),
            expires_at=datetime(2026, 5, 7, 14, 30, 55),
        )
        await store.write_action(entry)

        raw = await storage.read("actions/retag-notes/20260506-143055-foo.md")
        fm = extract_frontmatter(raw)
        assert "action_type" not in fm  # forbidden duplicate (§0.2)

    @pytest.mark.asyncio
    async def test_apply_status_round_trip(self, store: NoteStore) -> None:
        entry = models.ActionEntry(
            path="actions/create-concept/x.md",
            kind="create-concept",
            status="applied",
            action_schema_version="create-concept-v1",
            params={"concept": "ml", "title": "ML"},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6, 15, 0, 0),
            expires_at=datetime(2026, 5, 7),
            affected_paths=["concepts/active/ml.md"],
        )
        entry.execution.status = "ok"
        entry.execution.applied_at = datetime(2026, 5, 6, 15, 0, 0)
        entry.validation.status = "passed"

        await store.write_action(entry)
        got = await store.read_action("actions/create-concept/x.md")
        assert got is not None
        assert got.status == "applied"
        assert got.execution.status == "ok"
        assert got.execution.applied_at == datetime(2026, 5, 6, 15, 0, 0)
        assert got.validation.status == "passed"
        assert got.affected_paths == ["concepts/active/ml.md"]

    @pytest.mark.asyncio
    async def test_read_missing_action_returns_none(self, store: NoteStore) -> None:
        assert await store.read_action("actions/create-concept/missing.md") is None


class TestListExistingActionPaths:
    @pytest.mark.asyncio
    async def test_empty(self, store: NoteStore) -> None:
        result = await store.list_existing_action_paths("create-concept")
        assert result == set()

    @pytest.mark.asyncio
    async def test_lists_all_under_kind(self, store: NoteStore) -> None:
        for slug in ("a", "b", "c"):
            entry = models.ActionEntry(
                path=f"actions/create-concept/20260506-143012-{slug}.md",
                kind="create-concept",
                status="draft",
                action_schema_version="create-concept-v1",
                params={"concept": slug, "title": slug.upper()},
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
                expires_at=datetime(2026, 5, 7),
            )
            await store.write_action(entry)

        result = await store.list_existing_action_paths("create-concept")
        assert result == {
            "actions/create-concept/20260506-143012-a.md",
            "actions/create-concept/20260506-143012-b.md",
            "actions/create-concept/20260506-143012-c.md",
        }

    @pytest.mark.asyncio
    async def test_does_not_include_other_kinds(self, store: NoteStore) -> None:
        entry = models.ActionEntry(
            path="actions/retag-notes/x.md",
            kind="retag-notes",
            status="draft",
            action_schema_version="retag-notes-v1",
            params={"changes": []},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            expires_at=datetime(2026, 5, 7),
        )
        await store.write_action(entry)

        assert await store.list_existing_action_paths("create-concept") == set()


class TestGardenNoteFrontmatter:
    """RetagNotes mutates only the ``tags`` frontmatter field (Handoff §7.6)."""

    @pytest.mark.asyncio
    async def test_read_garden_tags(self, store: NoteStore, storage: FileSystemStorage) -> None:
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - ml\ncreated_at: 2026-05-06\n---\n# Foo\n\nbody.\n",
        )
        tags = await store.read_garden_tags("garden/seedling/foo.md")
        assert tags == ["ml"]

    @pytest.mark.asyncio
    async def test_read_garden_tags_missing_returns_empty(self, store: NoteStore) -> None:
        with pytest.raises(FileNotFoundError):
            await store.read_garden_tags("garden/seedling/missing.md")

    @pytest.mark.asyncio
    async def test_read_garden_tags_no_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await storage.write("garden/seedling/foo.md", "# Foo\n\nbody.\n")
        tags = await store.read_garden_tags("garden/seedling/foo.md")
        assert tags == []

    @pytest.mark.asyncio
    async def test_set_garden_tags_preserves_body_and_other_fields(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        original = (
            "---\n"
            "tags:\n  - ml\n"
            "aliases:\n  - foobar\n"
            "created_at: 2026-05-06\n"
            "---\n"
            "# Foo\n\n"
            "body content.\n"
        )
        await storage.write("garden/seedling/foo.md", original)
        await store.set_garden_tags("garden/seedling/foo.md", ["machine-learning"])

        raw = await storage.read("garden/seedling/foo.md")
        fm = extract_frontmatter(raw)
        assert fm["tags"] == ["machine-learning"]
        assert fm["aliases"] == ["foobar"]
        assert "created_at" in fm
        assert "# Foo" in raw
        assert "body content." in raw

    @pytest.mark.asyncio
    async def test_set_garden_tags_no_frontmatter_creates_one(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        await storage.write("garden/seedling/foo.md", "# Foo\n\nbody.\n")
        await store.set_garden_tags("garden/seedling/foo.md", ["machine-learning"])

        raw = await storage.read("garden/seedling/foo.md")
        fm = extract_frontmatter(raw)
        assert fm["tags"] == ["machine-learning"]
        assert "# Foo" in raw
        assert "body." in raw
