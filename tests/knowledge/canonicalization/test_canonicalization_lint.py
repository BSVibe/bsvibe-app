"""Tests for canon lint (Handoff §15.3 canon-lint plugin).

Detects:
- orphan garden tags — tags that don't resolve to any active concept or alias
- alias collisions — same alias used by ≥2 active concepts (also surfaces
  as ``ambiguous`` in resolver but lint creates a review-able proposal)
- redirect anomalies — tombstone chains that cycle or land on missing /
  non-active concepts
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lint import (
    LintFinding,
    find_alias_collisions,
    find_orphan_tags,
    find_redirect_anomalies,
    run_lint,
)
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
async def index(storage: FileSystemStorage) -> InMemoryCanonicalizationIndex:
    idx = InMemoryCanonicalizationIndex()
    await idx.initialize(storage)
    return idx


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


async def _seed_concept(
    store: NoteStore,
    cid: str,
    aliases: list[str] | None = None,
) -> None:
    await store.write_concept(
        models.ConceptEntry(
            concept_id=cid,
            path=f"concepts/active/{cid}.md",
            display=cid,
            aliases=aliases or [],
            created_at=datetime(2026, 5, 7),
            updated_at=datetime(2026, 5, 7),
        )
    )


async def _seed_tombstone(storage: FileSystemStorage, old_id: str, merged_into: str) -> None:
    text = f"---\nmerged_into: {merged_into}\nmerged_at: '2026-05-07T14:00:00'\n---\n# {old_id}\n"
    await storage.write(f"concepts/merged/{old_id}.md", text)


class TestFindOrphanTags:
    @pytest.mark.asyncio
    async def test_garden_tag_with_no_concept_is_orphan(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_concept(store, "machine-learning")
        await index.invalidate("concepts/active/machine-learning.md")

        # Garden notes referencing a non-existent concept
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - some-orphan\n  - machine-learning\n---\n# Foo\n",
        )

        findings = await find_orphan_tags(index, store)
        kinds = [(f.kind, f.payload.get("tag")) for f in findings]
        assert ("orphan_tag", "some-orphan") in kinds
        # Active concept tag NOT flagged
        assert ("orphan_tag", "machine-learning") not in kinds

    @pytest.mark.asyncio
    async def test_alias_match_is_not_orphan(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_concept(store, "machine-learning", aliases=["ml"])
        await index.invalidate("concepts/active/machine-learning.md")
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - ml\n---\n# Foo\n",
        )
        findings = await find_orphan_tags(index, store)
        assert findings == []

    @pytest.mark.asyncio
    async def test_tombstone_redirect_resolves_not_orphan(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        # Garden has 'self-host', tombstone redirects to active 'self-hosting'
        await _seed_concept(store, "self-hosting")
        await _seed_tombstone(storage, "self-host", "self-hosting")
        await index.invalidate("concepts/active/self-hosting.md")
        await index.invalidate("concepts/merged/self-host.md")
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - self-host\n---\n# Foo\n",
        )
        findings = await find_orphan_tags(index, store)
        assert findings == []

    @pytest.mark.asyncio
    async def test_payload_includes_garden_paths(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        for i in range(3):
            await storage.write(
                f"garden/seedling/n{i}.md",
                "---\ntags:\n  - orphaned\n---\n# n\n",
            )
        findings = await find_orphan_tags(index, store)
        assert len(findings) == 1
        f = findings[0]
        assert f.payload["tag"] == "orphaned"
        # Surfaces every garden file using the orphan tag
        assert len(f.payload["garden_paths"]) == 3


class TestFindAliasCollisions:
    @pytest.mark.asyncio
    async def test_two_concepts_sharing_alias(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
    ) -> None:
        await _seed_concept(store, "machine-learning", aliases=["ml-thing"])
        await _seed_concept(store, "meta-learning", aliases=["ml-thing"])
        await index.invalidate("concepts/active/machine-learning.md")
        await index.invalidate("concepts/active/meta-learning.md")

        findings = await find_alias_collisions(index)
        assert len(findings) == 1
        assert findings[0].kind == "alias_collision"
        assert findings[0].payload["alias"] == "ml-thing"
        assert set(findings[0].payload["concepts"]) == {
            "machine-learning",
            "meta-learning",
        }

    @pytest.mark.asyncio
    async def test_unique_alias_no_finding(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
    ) -> None:
        await _seed_concept(store, "machine-learning", aliases=["ml"])
        await _seed_concept(store, "deep-learning", aliases=["dl"])
        await index.invalidate("concepts/active/machine-learning.md")
        await index.invalidate("concepts/active/deep-learning.md")
        assert await find_alias_collisions(index) == []


class TestFindRedirectAnomalies:
    @pytest.mark.asyncio
    async def test_tombstone_to_missing_concept(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        # Tombstone points at nonexistent active concept
        await _seed_tombstone(storage, "old", "vanished")
        await index.invalidate("concepts/merged/old.md")

        findings = await find_redirect_anomalies(index)
        assert any(
            f.kind == "redirect_target_missing" and f.payload.get("old_id") == "old"
            for f in findings
        )

    @pytest.mark.asyncio
    async def test_tombstone_chain_cycle(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_tombstone(storage, "a", "b")
        await _seed_tombstone(storage, "b", "a")
        await index.invalidate("concepts/merged/a.md")
        await index.invalidate("concepts/merged/b.md")

        findings = await find_redirect_anomalies(index)
        assert any(f.kind == "redirect_cycle" for f in findings)

    @pytest.mark.asyncio
    async def test_tombstone_to_active_concept_clean(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_concept(store, "self-hosting")
        await _seed_tombstone(storage, "self-host", "self-hosting")
        await index.invalidate("concepts/active/self-hosting.md")
        await index.invalidate("concepts/merged/self-host.md")
        assert await find_redirect_anomalies(index) == []


class TestRunLint:
    @pytest.mark.asyncio
    async def test_aggregates_all_three(
        self,
        index: InMemoryCanonicalizationIndex,
        store: NoteStore,
        storage: FileSystemStorage,
    ) -> None:
        # Each kind seeded
        await _seed_concept(store, "ml-a", aliases=["dup"])
        await _seed_concept(store, "ml-b", aliases=["dup"])
        await _seed_tombstone(storage, "old", "vanished")
        await storage.write(
            "garden/seedling/foo.md",
            "---\ntags:\n  - orphan-thing\n---\n# Foo\n",
        )
        for path in (
            "concepts/active/ml-a.md",
            "concepts/active/ml-b.md",
            "concepts/merged/old.md",
        ):
            await index.invalidate(path)

        report = await run_lint(index, store)
        kinds = {f.kind for f in report.findings}
        assert "orphan_tag" in kinds
        assert "alias_collision" in kinds
        assert "redirect_target_missing" in kinds
        # Counts surface as report-level fields too
        assert report.orphan_tag_count >= 1
        assert report.alias_collision_count >= 1
        assert report.redirect_anomaly_count >= 1


class TestLintFindingShape:
    def test_finding_has_severity_and_payload(self) -> None:
        f = LintFinding(
            kind="orphan_tag",
            severity="warning",
            payload={"tag": "orphan", "garden_paths": ["garden/x.md"]},
        )
        assert f.severity == "warning"
        assert f.payload["tag"] == "orphan"
