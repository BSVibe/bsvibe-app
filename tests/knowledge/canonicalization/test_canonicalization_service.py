"""Tests for the slice-1 minimal canonicalization service.

Covers ``CreateConcept`` and ``RetagNotes`` only — the two action kinds in
scope for Vertical_Slices §2. Other kinds raise ``NotImplementedError``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter, extract_title
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
def service(storage: FileSystemStorage) -> CanonicalizationService:
    fixed_now = datetime(2026, 5, 6, 14, 30, 12)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        clock=lambda: fixed_now,
    )


class TestCreateActionDraft:
    @pytest.mark.asyncio
    async def test_create_concept_draft(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "machine-learning", "title": "Machine Learning"},
        )
        assert path == "actions/create-concept/20260506-143012-machine-learning.md"
        assert await storage.exists(path)

        # Frontmatter sanity check
        raw = await storage.read(path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "draft"
        assert fm["action_schema_version"] == "create-concept-v1"
        assert fm["params"] == {"concept": "machine-learning", "title": "Machine Learning"}

    @pytest.mark.asyncio
    async def test_retag_notes_draft(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="retag-notes",
            params={
                "changes": [
                    {
                        "path": "garden/seedling/foo.md",
                        "remove_tags": [],
                        "add_tags": ["machine-learning"],
                    }
                ]
            },
            slug="foo",
        )
        assert path == "actions/retag-notes/20260506-143012-foo.md"
        assert await storage.exists(path)

    @pytest.mark.asyncio
    async def test_collision_appends_suffix(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path1 = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        path2 = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        assert path1 != path2
        assert path2.endswith("-02.md")
        assert await storage.exists(path1)
        assert await storage.exists(path2)

    @pytest.mark.asyncio
    async def test_unsupported_kind_raises(self, service: CanonicalizationService) -> None:
        # split-concept is reserved for a later slice (Vertical_Slices §9 v1.1+)
        with pytest.raises(NotImplementedError, match="not yet supported"):
            await service.create_action_draft(
                kind="split-concept",
                params={"source": "ml", "new_concepts": []},
            )


class TestApplyCreateConcept:
    @pytest.mark.asyncio
    async def test_apply_creates_active_concept(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={
                "concept": "machine-learning",
                "title": "Machine Learning",
                "aliases": ["ML", "machine_learning"],
            },
        )
        result = await service.apply_action(path, actor="cli")

        assert result.final_status == "applied"
        assert "concepts/active/machine-learning.md" in result.affected_paths
        assert path in result.affected_paths

        # Concept note exists with correct shape
        raw = await storage.read("concepts/active/machine-learning.md")
        fm = extract_frontmatter(raw)
        assert fm.get("aliases") == ["ML", "machine_learning"]
        assert fm.get("source_action") == path
        assert "status" not in fm  # Handoff §3.1
        assert extract_title(raw) == "Machine Learning"

    @pytest.mark.asyncio
    async def test_apply_creates_concept_with_display_labels(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        """A create-concept action carrying per-locale ``display_labels`` writes
        them to the concept frontmatter (the write path for the localized graph
        node label); the H1 stays the English identifier."""
        path = await service.create_action_draft(
            kind="create-concept",
            params={
                "concept": "http-client",
                "title": "Http client",
                "display_labels": {"ko": "HTTP 클라이언트"},
            },
        )
        result = await service.apply_action(path, actor="cli")
        assert result.final_status == "applied"

        raw = await storage.read("concepts/active/http-client.md")
        assert extract_frontmatter(raw).get("display_labels") == {"ko": "HTTP 클라이언트"}
        assert extract_title(raw) == "Http client"

    @pytest.mark.asyncio
    async def test_apply_updates_action_note(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        await service.apply_action(path, actor="cli")

        raw = await storage.read(path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "applied"
        assert fm["execution"]["status"] == "ok"
        assert fm["execution"]["applied_at"] is not None
        assert fm["validation"]["status"] == "passed"
        assert "concepts/active/ml.md" in fm["affected_paths"]

    @pytest.mark.asyncio
    async def test_apply_blocks_when_concept_exists(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path1 = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        await service.apply_action(path1, actor="cli")

        path2 = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "Duplicate"},
        )
        result = await service.apply_action(path2, actor="cli")

        assert result.final_status == "blocked"
        assert any(
            "already_exists" in (b.get("payload", {}).get("reason") or "")
            for b in await _hard_blocks(storage, path2)
        )
        # Original concept untouched
        raw = await storage.read("concepts/active/ml.md")
        assert extract_title(raw) == "ML"

    @pytest.mark.asyncio
    async def test_apply_blocks_invalid_concept_id(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Bypass create_action_draft validation and write a malformed action
        store = NoteStore(storage)
        bad_path = "actions/create-concept/20260506-150000-x.md"
        await store.write_action(
            models.ActionEntry(
                path=bad_path,
                kind="create-concept",
                status="draft",
                action_schema_version="create-concept-v1",
                params={"concept": "Bad_ID", "title": "Bad"},
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
                expires_at=datetime(2026, 5, 7),
            )
        )
        result = await service.apply_action(bad_path, actor="cli")
        assert result.final_status == "blocked"


class TestApplyRetagNotes:
    @pytest.mark.asyncio
    async def test_apply_replaces_tag(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Setup: create the concept first
        cp = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "machine-learning", "title": "Machine Learning"},
        )
        await service.apply_action(cp, actor="cli")

        # Setup: existing garden note
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - ml\n---\n# Foo\n\nbody.\n",
        )

        # Action: retag
        rp = await service.create_action_draft(
            kind="retag-notes",
            params={
                "changes": [
                    {
                        "path": "garden/seedling/foo.md",
                        "remove_tags": ["ml"],
                        "add_tags": ["machine-learning"],
                    }
                ]
            },
            slug="foo",
        )
        result = await service.apply_action(rp, actor="cli")

        assert result.final_status == "applied"
        assert "garden/seedling/foo.md" in result.affected_paths

        raw = await storage.read("garden/seedling/foo.md")
        fm = extract_frontmatter(raw)
        assert fm["tags"] == ["machine-learning"]

    @pytest.mark.asyncio
    async def test_apply_blocks_when_add_tag_not_active(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - ml\n---\n# Foo\n",
        )
        rp = await service.create_action_draft(
            kind="retag-notes",
            params={
                "changes": [
                    {
                        "path": "garden/seedling/foo.md",
                        "remove_tags": [],
                        "add_tags": ["does-not-exist"],
                    }
                ]
            },
            slug="foo",
        )
        result = await service.apply_action(rp, actor="cli")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_apply_blocks_non_garden_path(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Setup concept
        cp = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        await service.apply_action(cp, actor="cli")

        rp = await service.create_action_draft(
            kind="retag-notes",
            params={
                "changes": [
                    {
                        "path": "raw/foo.md",  # outside garden/
                        "remove_tags": [],
                        "add_tags": ["ml"],
                    }
                ]
            },
            slug="foo",
        )
        result = await service.apply_action(rp, actor="cli")
        assert result.final_status == "blocked"


class TestApplyAlreadyApplied:
    @pytest.mark.asyncio
    async def test_re_apply_is_idempotent_noop(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "ml", "title": "ML"},
        )
        first = await service.apply_action(path, actor="cli")
        assert first.final_status == "applied"

        second = await service.apply_action(path, actor="cli")
        assert second.final_status == "applied"
        # Action note still exists, still applied
        raw = await storage.read(path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "applied"


async def _hard_blocks(storage: FileSystemStorage, action_path: str) -> list[dict]:
    raw = await storage.read(action_path)
    fm = extract_frontmatter(raw)
    return list(fm.get("validation", {}).get("hard_blocks") or [])
