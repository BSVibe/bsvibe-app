"""Tests for DeterministicProposer (Handoff §12 preprocessing layer)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.proposals import DeterministicProposer
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
    fixed_now = datetime(2026, 5, 6, 15, 0, 12)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: fixed_now,
    )


@pytest.fixture
def proposer(service: CanonicalizationService) -> DeterministicProposer:
    fixed_now = datetime(2026, 5, 6, 15, 0, 12)
    return DeterministicProposer(
        index=service._index,
        store=service._store,
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


class TestNgramJaccard:
    def test_identical_strings(self) -> None:
        assert DeterministicProposer.ngram_jaccard("self-hosting", "self-hosting") == 1.0

    def test_completely_different(self) -> None:
        assert DeterministicProposer.ngram_jaccard("python", "rust") == pytest.approx(0.0)

    def test_close_variants(self) -> None:
        # self-hosting vs self-host should score high
        score = DeterministicProposer.ngram_jaccard("self-hosting", "self-host")
        assert score > 0.5

    def test_unrelated_low_score(self) -> None:
        score = DeterministicProposer.ngram_jaccard("machine-learning", "deep-learning")
        # Some overlap on "-learning" but not enough to merge
        assert 0.0 < score < 0.7


class TestEmptyVault:
    @pytest.mark.asyncio
    async def test_no_concepts_returns_empty(self, proposer: DeterministicProposer) -> None:
        assert await proposer.generate() == []

    @pytest.mark.asyncio
    async def test_single_concept_returns_empty(
        self, proposer: DeterministicProposer, service: CanonicalizationService
    ) -> None:
        await _seed_active(service, "self-hosting")
        assert await proposer.generate() == []


class TestPairwiseSimilarity:
    @pytest.mark.asyncio
    async def test_close_pair_generates_merge_proposal(
        self,
        proposer: DeterministicProposer,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        proposals = await proposer.generate()
        assert len(proposals) == 1

        proposal_path = proposals[0]
        assert proposal_path.startswith("proposals/merge-concepts/")
        raw = await storage.read(proposal_path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "pending"
        assert fm["strategy"] == "deterministic"
        assert fm["generator"] == "deterministic-v1"
        assert "evidence" in fm

    @pytest.mark.asyncio
    async def test_unrelated_pair_no_proposal(
        self, proposer: DeterministicProposer, service: CanonicalizationService
    ) -> None:
        await _seed_active(service, "machine-learning")
        await _seed_active(service, "vault-warden")
        assert await proposer.generate() == []


class TestCanonicalSelection:
    @pytest.mark.asyncio
    async def test_higher_garden_frequency_becomes_canonical(
        self,
        proposer: DeterministicProposer,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        # self-hosting used in 5 garden notes, self-host in 1
        for i in range(5):
            await storage.write(
                f"garden/seedling/note{i}.md",
                "---\ntags:\n  - self-hosting\n---\n# Note\n",
            )
        await storage.write(
            "garden/seedling/other.md",
            "---\ntags:\n  - self-host\n---\n# Other\n",
        )

        proposals = await proposer.generate()
        assert len(proposals) == 1

        raw = await storage.read(proposals[0])
        fm = extract_frontmatter(raw)
        # action_drafts: an action draft will be linked
        assert len(fm.get("action_drafts", [])) == 1
        action_path = fm["action_drafts"][0]
        action_raw = await storage.read(action_path)
        action_fm = extract_frontmatter(action_raw)
        assert action_fm["params"]["canonical"] == "self-hosting"
        assert "self-host" in action_fm["params"]["merge"]


class TestEvidenceShape:
    @pytest.mark.asyncio
    async def test_evidence_has_required_envelope(
        self,
        proposer: DeterministicProposer,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")
        proposals = await proposer.generate()
        raw = await storage.read(proposals[0])
        fm = extract_frontmatter(raw)
        evidence = fm["evidence"]
        assert len(evidence) >= 1
        for ev in evidence:
            assert "kind" in ev
            assert "schema_version" in ev
            assert ev["source"] == "deterministic"
            assert "observed_at" in ev
            assert "producer" in ev


class TestNoDuplicateProposals:
    @pytest.mark.asyncio
    async def test_re_running_does_not_duplicate(
        self,
        proposer: DeterministicProposer,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        await _seed_active(service, "self-hosting")
        await _seed_active(service, "self-host")

        first = await proposer.generate()
        second = await proposer.generate()
        # Same logical proposal — should not create a second draft
        assert len(first) == 1
        assert second == []  # already-pending case suppresses duplicate
