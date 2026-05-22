"""Tests for ProposalEntry + NoteStore proposal CRUD (Handoff §5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models, paths
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


@pytest.fixture
def store(storage: FileSystemStorage) -> NoteStore:
    return NoteStore(storage)


class TestProposalPaths:
    def test_build_proposal_path(self) -> None:
        dt = datetime(2026, 5, 6, 15, 0, 12)
        result = paths.build_proposal_path("merge-concepts", dt, "self-hosting")
        assert result == "proposals/merge-concepts/20260506-150012-self-hosting.md"

    def test_invalid_proposal_kind(self) -> None:
        dt = datetime(2026, 5, 6, 15, 0, 12)
        with pytest.raises(ValueError, match="unknown proposal kind"):
            paths.build_proposal_path("not-a-kind", dt, "x")

    def test_known_proposal_kinds(self) -> None:
        # Per Handoff §5
        expected = {
            "merge-concepts",
            "create-concept",
            "retag-notes",
            "policy-update",
            "policy-conflict",
            "decision-review",
        }
        assert set(paths.PROPOSAL_KINDS) == expected


class TestProposalEntryShape:
    def test_minimal_construction(self) -> None:
        entry = models.ProposalEntry(
            path="proposals/merge-concepts/20260506-150012-self-hosting.md",
            kind="merge-concepts",
            status="pending",
            strategy="deterministic",
            generator="deterministic-v1",
            generator_version="canonicalization-generator-v1",
            proposal_score=0.91,
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            expires_at=datetime(2026, 5, 13),
        )
        assert entry.evidence == []
        assert entry.action_drafts == []
        assert entry.result_actions == []


class TestReadWriteProposal:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: NoteStore, storage: FileSystemStorage) -> None:
        entry = models.ProposalEntry(
            path="proposals/merge-concepts/20260506-150012-self-hosting.md",
            kind="merge-concepts",
            status="pending",
            strategy="deterministic",
            generator="deterministic-v1",
            generator_version="canonicalization-generator-v1",
            proposal_score=0.85,
            created_at=datetime(2026, 5, 6, 15, 0, 12),
            updated_at=datetime(2026, 5, 6, 15, 0, 12),
            expires_at=datetime(2026, 5, 13, 15, 0, 12),
            evidence=[
                {
                    "kind": "alias_exact",
                    "schema_version": "alias-exact-v1",
                    "source": "deterministic",
                    "observed_at": "2026-05-06T15:00:12",
                    "producer": "deterministic-v1",
                    "payload": {"alias": "self-host", "matches": ["self-hosting"]},
                }
            ],
            action_drafts=["actions/merge-concepts/20260506-150042-self-hosting.md"],
        )
        await store.write_proposal(entry)
        got = await store.read_proposal(entry.path)
        assert got is not None
        assert got.status == "pending"
        assert got.proposal_score == 0.85
        assert got.evidence[0]["kind"] == "alias_exact"
        assert got.action_drafts == ["actions/merge-concepts/20260506-150042-self-hosting.md"]

    @pytest.mark.asyncio
    async def test_kind_path_derived_not_in_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        # Per Handoff §0.2 — proposal_type forbidden
        entry = models.ProposalEntry(
            path="proposals/merge-concepts/x.md",
            kind="merge-concepts",
            status="pending",
            strategy="deterministic",
            generator="deterministic-v1",
            generator_version="v1",
            proposal_score=0.5,
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            expires_at=datetime(2026, 5, 13),
        )
        await store.write_proposal(entry)
        raw = await storage.read(entry.path)
        fm = extract_frontmatter(raw)
        assert "proposal_type" not in fm

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, store: NoteStore) -> None:
        assert await store.read_proposal("proposals/merge-concepts/missing.md") is None


class TestListExistingProposalPaths:
    @pytest.mark.asyncio
    async def test_empty(self, store: NoteStore) -> None:
        assert await store.list_existing_proposal_paths("merge-concepts") == set()

    @pytest.mark.asyncio
    async def test_lists_all_under_kind(self, store: NoteStore) -> None:
        for slug in ("a", "b"):
            entry = models.ProposalEntry(
                path=f"proposals/merge-concepts/20260506-150012-{slug}.md",
                kind="merge-concepts",
                status="pending",
                strategy="deterministic",
                generator="deterministic-v1",
                generator_version="v1",
                proposal_score=0.5,
                created_at=datetime(2026, 5, 6),
                updated_at=datetime(2026, 5, 6),
                expires_at=datetime(2026, 5, 13),
            )
            await store.write_proposal(entry)
        result = await store.list_existing_proposal_paths("merge-concepts")
        assert result == {
            "proposals/merge-concepts/20260506-150012-a.md",
            "proposals/merge-concepts/20260506-150012-b.md",
        }
