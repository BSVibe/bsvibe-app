"""Tests for MergeConcepts action (Handoff §7.2).

Validates the four key effects:
- Update canonical aliases (alias_policy)
- Create tombstones at concepts/merged/<old-id>.md (tombstone_policy)
- Retag garden notes from merged ids to canonical (retag_policy)
- Hard Blocks: invalid params, non-active targets, self-merge, redirect cycle
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
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
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: fixed_now,
    )


async def _seed_active(
    service: CanonicalizationService, concept_id: str, aliases: list[str] | None = None
) -> None:
    path = await service.create_action_draft(
        kind="create-concept",
        params={
            "concept": concept_id,
            "title": concept_id,
            "aliases": aliases or [],
        },
    )
    await service.apply_action(path, actor="test")


class TestCreateMergeDraft:
    @pytest.mark.asyncio
    async def test_basic_draft(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={
                "canonical": "self-hosting",
                "merge": ["self-host"],
            },
        )
        assert path.startswith("actions/merge-concepts/")
        assert path.endswith("self-hosting.md")
        raw = await storage.read(path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "draft"
        assert fm["params"]["canonical"] == "self-hosting"
        assert fm["params"]["merge"] == ["self-host"]


class TestMergeValidation:
    @pytest.mark.asyncio
    async def test_canonical_must_be_active(self, service: CanonicalizationService) -> None:
        await _seed_active(service, "self-host")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "nonexistent", "merge": ["self-host"]},
        )
        result = await service.apply_action(path, actor="test")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_merge_target_must_be_active(self, service: CanonicalizationService) -> None:
        await _seed_active(service, "self-hosting")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["nonexistent"]},
        )
        result = await service.apply_action(path, actor="test")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_canonical_in_merge_blocked(self, service: CanonicalizationService) -> None:
        await _seed_active(service, "self-hosting")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-hosting"]},
        )
        result = await service.apply_action(path, actor="test")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_empty_merge_list_blocked(self, service: CanonicalizationService) -> None:
        await _seed_active(service, "self-hosting")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": []},
        )
        result = await service.apply_action(path, actor="test")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_invalid_concept_id_blocked(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Bypass create_action_draft slug derivation (which raises early)
        # and write a malformed action directly to test the validate stage.
        store = NoteStore(storage)
        bad_path = "actions/merge-concepts/20260506-150000-x.md"
        await store.write_action(
            models.ActionEntry(
                path=bad_path,
                kind="merge-concepts",
                status="draft",
                action_schema_version="merge-concepts-v1",
                params={"canonical": "Bad_ID", "merge": ["x"]},
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
                expires_at=datetime(2026, 5, 7),
            )
        )
        result = await service.apply_action(bad_path, actor="test")
        assert result.final_status == "blocked"


class TestMergeEffects:
    @pytest.mark.asyncio
    async def test_alias_union_default_policy(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting", aliases=["selfhost-existing"])
        await _seed_active(service, "self-host", aliases=["self-h"])

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        result = await service.apply_action(path, actor="test")
        assert result.final_status == "applied"

        canonical = await storage.read("concepts/active/self-hosting.md")
        fm = extract_frontmatter(canonical)
        # Default alias_policy: add merged ids as aliases + preserve existing
        assert "self-host" in fm["aliases"]
        assert "self-h" in fm["aliases"]
        assert "selfhost-existing" in fm["aliases"]

    @pytest.mark.asyncio
    async def test_tombstone_created_with_correct_redirect(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        await service.apply_action(path, actor="test")

        # Old active note removed
        assert not await storage.exists("concepts/active/self-host.md")
        # Tombstone created
        assert await storage.exists("concepts/merged/self-host.md")
        ts = await storage.read("concepts/merged/self-host.md")
        fm = extract_frontmatter(ts)
        assert fm["merged_into"] == "self-hosting"
        assert fm["source_action"] == path

    @pytest.mark.asyncio
    async def test_garden_retag_default_policy(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        # Garden notes referencing both old ids
        await storage.write(
            "garden/seedling/a.md",
            "---\ntags:\n  - self-host\n  - other\n---\n# A\n",
        )
        await storage.write(
            "garden/seedling/b.md",
            "---\ntags:\n  - self-hosting\n  - self-host\n---\n# B\n",
        )

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        await service.apply_action(path, actor="test")

        a = extract_frontmatter(await storage.read("garden/seedling/a.md"))
        assert "self-host" not in a["tags"]
        assert "self-hosting" in a["tags"]
        assert "other" in a["tags"]

        b = extract_frontmatter(await storage.read("garden/seedling/b.md"))
        # Dedup — single entry not duplicated
        assert b["tags"].count("self-hosting") == 1
        assert "self-host" not in b["tags"]

    @pytest.mark.asyncio
    async def test_affected_paths_includes_concept_tombstone_garden(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")
        await storage.write(
            "garden/seedling/a.md",
            "---\ntags:\n  - self-host\n---\n# A\n",
        )

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        result = await service.apply_action(path, actor="test")
        assert "concepts/active/self-hosting.md" in result.affected_paths
        assert "concepts/merged/self-host.md" in result.affected_paths
        assert "garden/seedling/a.md" in result.affected_paths

    @pytest.mark.asyncio
    async def test_resolver_redirects_after_merge(self, service: CanonicalizationService) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        path = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        await service.apply_action(path, actor="test")

        # Tombstone redirect: resolver returns canonical on the old id
        canonical = await service.resolve_and_canonicalize("self-host")
        assert canonical == "self-hosting"


class TestMergePolicyOverrides:
    @pytest.mark.asyncio
    async def test_no_alias_addition_when_disabled(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={
                "canonical": "self-hosting",
                "merge": ["self-host"],
                "alias_policy": {
                    "add_merged_ids_as_aliases": False,
                    "preserve_existing_aliases": True,
                },
            },
        )
        await service.apply_action(path, actor="test")

        canonical = await storage.read("concepts/active/self-hosting.md")
        fm = extract_frontmatter(canonical)
        # When add_merged_ids_as_aliases is False, no aliases section / empty
        assert "self-host" not in (fm.get("aliases") or [])

    @pytest.mark.asyncio
    async def test_no_garden_retag_when_disabled(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")
        await storage.write(
            "garden/seedling/a.md",
            "---\ntags:\n  - self-host\n---\n# A\n",
        )
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={
                "canonical": "self-hosting",
                "merge": ["self-host"],
                "retag_policy": {"update_garden_tags": False},
            },
        )
        await service.apply_action(path, actor="test")
        # Garden tag stays as-is
        a = extract_frontmatter(await storage.read("garden/seedling/a.md"))
        assert a["tags"] == ["self-host"]

    @pytest.mark.asyncio
    async def test_no_tombstone_when_disabled(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")
        path = await service.create_action_draft(
            kind="merge-concepts",
            params={
                "canonical": "self-hosting",
                "merge": ["self-host"],
                "tombstone_policy": {"create_merged_notes": False},
            },
        )
        await service.apply_action(path, actor="test")
        # No tombstone, but old active still removed (since merge happened)
        assert not await storage.exists("concepts/merged/self-host.md")
        assert not await storage.exists("concepts/active/self-host.md")
