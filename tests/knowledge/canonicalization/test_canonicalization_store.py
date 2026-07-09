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
    async def test_write_concept_carries_note_type_in_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        """Lift E26 — a concept's ``note_type`` (Pattern / Principle /
        TechInsight / DomainModel) MUST round-trip through the frontmatter.

        Pre-E26 the seedling's E20 ``type:`` field was dropped during
        promotion: ``concepts/active/<slug>.md`` only carried
        ``aliases / created_at / updated_at / source_action``. The founder
        sees a 400+ concept pool that all reads as "one type" with no way
        to distinguish patterns from domain models from infra principles.
        """
        entry = models.ConceptEntry(
            concept_id="oauth-loopback-pkce",
            path="concepts/active/oauth-loopback-pkce.md",
            display="OAuth loopback PKCE",
            aliases=[],
            created_at=datetime(2026, 6, 14),
            updated_at=datetime(2026, 6, 14),
            note_type="Pattern",
        )
        await store.write_concept(entry)

        raw = await storage.read("concepts/active/oauth-loopback-pkce.md")
        fm = extract_frontmatter(raw)
        assert fm.get("type") == "Pattern"

        # Round-trip — read_concept returns the typed entry.
        got = await store.read_concept("oauth-loopback-pkce")
        assert got is not None
        assert got.note_type == "Pattern"

    @pytest.mark.asyncio
    async def test_write_concept_without_type_omits_field(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        """E26 back-compat — a concept written without a ``note_type``
        keeps the pre-E26 frontmatter shape (no ``type`` key)."""
        entry = models.ConceptEntry(
            concept_id="untyped",
            path="concepts/active/untyped.md",
            display="Untyped",
            aliases=[],
            created_at=datetime(2026, 6, 14),
            updated_at=datetime(2026, 6, 14),
        )
        await store.write_concept(entry)
        fm = extract_frontmatter(await storage.read("concepts/active/untyped.md"))
        assert "type" not in fm
        got = await store.read_concept("untyped")
        assert got is not None
        assert got.note_type is None

    @pytest.mark.asyncio
    async def test_write_concept_carries_display_labels_in_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        """Per-locale display label: the concept ID + H1 stay the stable English
        identifier ('http-client' / 'Http client'), but a localized display label
        rides in frontmatter so a KO workspace graph can render the node in Korean
        without fragmenting concept identity (founder decision, 2026-07)."""
        entry = models.ConceptEntry(
            concept_id="http-client",
            path="concepts/active/http-client.md",
            display="Http client",
            aliases=[],
            created_at=datetime(2026, 7, 9),
            updated_at=datetime(2026, 7, 9),
            display_labels={"ko": "HTTP 클라이언트"},
        )
        await store.write_concept(entry)

        raw = await storage.read("concepts/active/http-client.md")
        fm = extract_frontmatter(raw)
        assert fm.get("display_labels") == {"ko": "HTTP 클라이언트"}
        # Identity is unchanged: H1 stays the English display, not the label.
        assert extract_title(raw) == "Http client"

        got = await store.read_concept("http-client")
        assert got is not None
        assert got.display == "Http client"
        assert got.display_labels == {"ko": "HTTP 클라이언트"}

    @pytest.mark.asyncio
    async def test_write_concept_without_labels_omits_field(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        """Back-compat — a concept written without display labels keeps the
        prior frontmatter shape (no ``display_labels`` key) and reads back as {}."""
        entry = models.ConceptEntry(
            concept_id="unlabelled",
            path="concepts/active/unlabelled.md",
            display="Unlabelled",
            aliases=[],
            created_at=datetime(2026, 7, 9),
            updated_at=datetime(2026, 7, 9),
        )
        await store.write_concept(entry)
        fm = extract_frontmatter(await storage.read("concepts/active/unlabelled.md"))
        assert "display_labels" not in fm
        got = await store.read_concept("unlabelled")
        assert got is not None
        assert got.display_labels == {}

    @pytest.mark.asyncio
    async def test_update_display_labels_preserves_body(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        """Backfill primitive: add a localized label to an EXISTING concept
        WITHOUT clobbering its synthesized body (framing + MOC) or its identity
        (id / H1). Merges into any labels already present."""
        entry = models.ConceptEntry(
            concept_id="http-client",
            path="concepts/active/http-client.md",
            display="Http client",
            aliases=["httpclient"],
            created_at=datetime(2026, 7, 9),
            updated_at=datetime(2026, 7, 9),
            note_type="Pattern",
        )
        await store.write_concept(entry, initial_body="HTTP 요청을 보내는 클라이언트.")

        await store.update_concept_display_labels("http-client", {"ko": "HTTP 클라이언트"})

        got = await store.read_concept("http-client")
        assert got is not None
        assert got.display_labels == {"ko": "HTTP 클라이언트"}
        # Identity + body + other frontmatter survived the read-modify-write.
        assert got.display == "Http client"
        assert got.note_type == "Pattern"
        assert got.aliases == ["httpclient"]
        raw = await storage.read("concepts/active/http-client.md")
        assert "HTTP 요청을 보내는 클라이언트." in raw

    @pytest.mark.asyncio
    async def test_update_display_labels_missing_concept_is_noop(self, store: NoteStore) -> None:
        """Updating a concept that doesn't exist is a safe no-op (no write)."""
        await store.update_concept_display_labels("nope", {"ko": "없음"})
        assert await store.read_concept("nope") is None

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


class TestReadGardenSummary:
    """KG Lift 1 — (title, excerpt) for composing a concept hub body."""

    @pytest.mark.asyncio
    async def test_returns_title_and_first_body_line(self, store: NoteStore) -> None:
        await store._storage.write(
            "garden/seedling/x.md",
            "---\ntags:\n  - t\n---\n# A Title\n\nThe working statement line.\nMore detail.\n",
        )
        summary = await store.read_garden_summary("garden/seedling/x.md")
        assert summary == ("A Title", "The working statement line.")

    @pytest.mark.asyncio
    async def test_excerpt_skips_heading_lines(self, store: NoteStore) -> None:
        await store._storage.write(
            "garden/seedling/y.md",
            "---\ntags: []\n---\n# Heading\n\n## Subheading\n\nReal content here.\n",
        )
        _title, excerpt = await store.read_garden_summary("garden/seedling/y.md")
        assert excerpt == "Real content here."

    @pytest.mark.asyncio
    async def test_missing_note_returns_none(self, store: NoteStore) -> None:
        assert await store.read_garden_summary("garden/seedling/nope.md") is None
