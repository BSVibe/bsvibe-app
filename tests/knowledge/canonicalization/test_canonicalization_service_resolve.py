"""Tests for slice-2 service resolve_and_canonicalize + index integration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
async def service(storage: FileSystemStorage) -> CanonicalizationService:
    fixed_now = datetime(2026, 5, 6, 14, 30, 12)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    store = NoteStore(storage)
    return CanonicalizationService(
        store=store,
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: fixed_now,
    )


class TestApplyKeepsIndexFresh:
    async def test_apply_create_concept_invalidates_index(
        self, service: CanonicalizationService
    ) -> None:
        # Index initially empty
        assert await service._index.get_active_concept("ml") is None

        path = await service.create_action_draft(
            kind="create-concept", params={"concept": "ml", "title": "ML"}
        )
        await service.apply_action(path, actor="cli")

        # Index now sees it
        assert await service._index.get_active_concept("ml") is not None

    async def test_pending_draft_visible_after_create(
        self, service: CanonicalizationService
    ) -> None:
        await service.create_action_draft(
            kind="create-concept", params={"concept": "ml", "title": "ML"}
        )
        # Service must have invalidated the action path on the index too,
        # so resolver sees it as pending.
        result = await service._index.find_pending_concept_draft("ml")
        assert result is not None


class TestResolveAndCanonicalizeResolved:
    async def test_existing_concept_returns_id(self, service: CanonicalizationService) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "machine-learning", "title": "ML", "aliases": ["ML"]},
        )
        await service.apply_action(path, actor="cli")

        canonical = await service.resolve_and_canonicalize("ML")
        assert canonical == "machine-learning"

    async def test_normalization_finds_existing(self, service: CanonicalizationService) -> None:
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "machine-learning", "title": "ML"},
        )
        await service.apply_action(path, actor="cli")

        assert await service.resolve_and_canonicalize("Machine Learning") == "machine-learning"
        assert await service.resolve_and_canonicalize("machine_learning") == "machine-learning"


class TestResolveAndCanonicalizeNewCandidate:
    async def test_auto_apply_creates_active_concept(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        canonical = await service.resolve_and_canonicalize(
            "Self Hosting", raw_source="raw/local/sample.md"
        )
        assert canonical == "self-hosting"

        # Concept now exists
        assert await storage.exists("concepts/active/self-hosting.md")
        # Action draft exists in vault, status applied
        action_files = await storage.list_files("actions/create-concept")
        assert len(action_files) == 1
        raw = await storage.read(action_files[0])
        fm = extract_frontmatter(raw)
        assert fm["status"] == "applied"
        assert fm["params"]["concept"] == "self-hosting"

    async def test_disabled_auto_apply_returns_none_but_drafts(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        canonical = await service.resolve_and_canonicalize(
            "Self Hosting", raw_source="raw/local/sample.md", auto_apply=False
        )
        assert canonical is None

        # Draft created but not applied
        action_files = await storage.list_files("actions/create-concept")
        assert len(action_files) == 1
        raw = await storage.read(action_files[0])
        fm = extract_frontmatter(raw)
        assert fm["status"] == "draft"


class TestResolveAndCanonicalizePendingCandidate:
    async def test_second_sighting_links_evidence_does_not_double_draft(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        # First call without auto-apply leaves a draft
        c1 = await service.resolve_and_canonicalize(
            "Self Hosting",
            raw_source="raw/local/a.md",
            auto_apply=False,
        )
        assert c1 is None

        action_files = await storage.list_files("actions/create-concept")
        assert len(action_files) == 1
        existing_draft = action_files[0]

        # Second sighting: same normalized tag, different raw source
        c2 = await service.resolve_and_canonicalize(
            "self-hosting",
            raw_source="raw/local/b.md",
            auto_apply=False,
        )
        # Pending candidate path: caller decides not to write canonical id yet.
        assert c2 is None

        # No second draft created
        action_files2 = await storage.list_files("actions/create-concept")
        assert action_files2 == [existing_draft]

        # Evidence appended to the existing draft
        raw = await storage.read(existing_draft)
        fm = extract_frontmatter(raw)
        evidence = fm.get("evidence") or []
        assert len(evidence) == 1
        ev = evidence[0]
        assert ev["kind"] == "ingest_pending_candidate"
        assert ev["source"] == "system"
        assert ev["payload"]["raw_tag"] == "self-hosting"
        assert ev["payload"]["normalized_tag"] == "self-hosting"
        assert ev["payload"]["raw_source"] == "raw/local/b.md"


class TestResolveAndCanonicalizeBlocked:
    async def test_ambiguous_returns_none(self, service: CanonicalizationService) -> None:
        # Set up alias collision: two concepts share alias "ml-thing"
        for cid in ("machine-learning", "meta-learning"):
            path = await service.create_action_draft(
                kind="create-concept",
                params={"concept": cid, "title": cid, "aliases": ["ml-thing"]},
            )
            await service.apply_action(path, actor="cli")

        result = await service.resolve_and_canonicalize("ml-thing")
        assert result is None

    async def test_deprecated_returns_none(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        await storage.write(
            "concepts/deprecated/old-thing.md",
            "---\ndeprecated_at: '2026-05-06T14:45:00'\n---\n# old\n",
        )
        await service._index.invalidate("concepts/deprecated/old-thing.md")

        assert await service.resolve_and_canonicalize("old-thing") is None

    async def test_garbage_input_returns_none(self, service: CanonicalizationService) -> None:
        assert await service.resolve_and_canonicalize("...") is None
        assert await service.resolve_and_canonicalize("") is None
