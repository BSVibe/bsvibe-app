"""Tests for BalancedProposer (Handoff §11 balanced strategy + §12).

Slice 4 ships the schema/wiring with pluggable embedder + verifier
callables. Tests use in-process mocks; real Embedder/LiteLLM integration
happens at gateway boot (slice 5).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.proposals import BalancedProposer
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
    fixed_now = datetime(2026, 5, 7, 14, 0, 0)
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: fixed_now,
    )


async def _seed(service: CanonicalizationService, concept_id: str) -> None:
    path = await service.create_action_draft(
        kind="create-concept",
        params={"concept": concept_id, "title": concept_id, "aliases": []},
    )
    await service.apply_action(path, actor="test")


def _embed_with_table(table: dict[str, list[float]]):
    async def _embedder(ids: list[str]) -> list[list[float]]:
        return [table[i] for i in ids]

    return _embedder


def _make_verifier(verdicts: dict[tuple[str, str], dict]):
    async def _verify(a: str, b: str) -> dict:
        return (
            verdicts.get((a, b))
            or verdicts.get((b, a))
            or {
                "verdict": "different_concept",
                "confidence": 0.5,
            }
        )

    return _verify


class TestBalancedFallsBackToDeterministic:
    @pytest.mark.asyncio
    async def test_no_embedder_no_verifier_matches_deterministic(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed(service, "self-hosting")
        await _seed(service, "self-host")

        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        proposals = await proposer.generate()
        assert len(proposals) == 1
        fm = extract_frontmatter(await storage.read(proposals[0]))
        assert fm["strategy"] == "balanced"
        # No model evidence when no embedder/verifier
        kinds = [e["kind"] for e in fm["evidence"]]
        assert "embedding_knn" not in kinds
        assert "llm_verify" not in kinds


class TestEmbeddingKnn:
    @pytest.mark.asyncio
    async def test_embedding_neighbors_added_as_candidates(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Two concepts that are NOT lexically similar but have very close
        # embeddings — deterministic alone wouldn't propose them.
        await _seed(service, "auth")
        await _seed(service, "credentials")

        embedder = _embed_with_table(
            {
                "auth": [1.0, 0.0],
                "credentials": [0.99, 0.05],
            }
        )

        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            embedder=embedder,
            embedding_top_k=3,
            embedding_threshold=0.9,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        proposals = await proposer.generate()
        assert len(proposals) == 1
        fm = extract_frontmatter(await storage.read(proposals[0]))
        kinds = [e["kind"] for e in fm["evidence"]]
        assert "embedding_knn" in kinds
        embedding_ev = next(e for e in fm["evidence"] if e["kind"] == "embedding_knn")
        # Per §2 — embedding output is model-derived
        assert embedding_ev["source"] == "model"
        assert "cosine" in embedding_ev["payload"]
        assert embedding_ev["payload"]["cosine"] > 0.9

    @pytest.mark.asyncio
    async def test_low_similarity_does_not_create_proposal(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed(service, "auth")
        await _seed(service, "kubernetes")

        embedder = _embed_with_table({"auth": [1.0, 0.0], "kubernetes": [0.0, 1.0]})
        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            embedder=embedder,
            embedding_threshold=0.9,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        assert await proposer.generate() == []


class TestLlmVerify:
    @pytest.mark.asyncio
    async def test_verifier_evidence_attached(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        await _seed(service, "self-hosting")
        await _seed(service, "self-host")

        verifier = _make_verifier(
            {("self-host", "self-hosting"): {"verdict": "same_concept", "confidence": 0.91}}
        )

        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            verifier=verifier,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        proposals = await proposer.generate()
        assert len(proposals) == 1
        fm = extract_frontmatter(await storage.read(proposals[0]))
        verify_evs = [e for e in fm["evidence"] if e["kind"] == "llm_verify"]
        assert len(verify_evs) >= 1
        ev = verify_evs[0]
        # Per §13 — LLM output MUST be source=model
        assert ev["source"] == "model"
        assert ev["payload"]["verdict"] == "same_concept"
        assert ev["payload"]["confidence"] == 0.91

    @pytest.mark.asyncio
    async def test_verifier_different_concept_does_not_drop_proposal(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        # Slice 4: balanced still creates the proposal even when verify says
        # "different" — the human gets the evidence and decides. (Slice 5
        # may layer a "drop on strong negative verdict" optimisation.)
        await _seed(service, "self-hosting")
        await _seed(service, "self-host")

        verifier = _make_verifier(
            {
                ("self-host", "self-hosting"): {
                    "verdict": "different_concept",
                    "confidence": 0.85,
                }
            }
        )
        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            verifier=verifier,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        proposals = await proposer.generate()
        assert len(proposals) == 1


class TestCannotLinkSkipsCandidates:
    @pytest.mark.asyncio
    async def test_strong_cannot_link_filters_pair(
        self,
        service: CanonicalizationService,
        storage: FileSystemStorage,
    ) -> None:
        from backend.knowledge.canonicalization import models as m

        await _seed(service, "ci")
        await _seed(service, "cd")
        # Even though n-gram + embedding might cluster these, an active
        # strong cannot-link should suppress proposal generation.
        await service._store.write_decision(
            m.DecisionEntry(
                path="decisions/cannot-link/20260507-140000-ci-cd.md",
                kind="cannot-link",
                status="active",
                maturity="seedling",
                decision_schema_version="cannot-link-v1",
                subjects=("ci", "cd"),
                base_confidence=0.95,
                last_confirmed_at=datetime(2026, 5, 7),
                decay_profile="definitional",
                decay_halflife_days=None,
                valid_from=datetime(2026, 5, 7),
                created_at=datetime(2026, 5, 7),
                updated_at=datetime(2026, 5, 7),
            )
        )
        await service._index.invalidate("decisions/cannot-link/20260507-140000-ci-cd.md")

        from backend.knowledge.canonicalization.decisions import DecisionMemory

        proposer = BalancedProposer(
            index=service._index,
            store=service._store,
            decisions=DecisionMemory(index=service._index, store=service._store),
            cannot_link_threshold=0.85,
            clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
        )
        # ci/cd Jaccard is high; without the decision filter we'd see one
        # proposal. With the decision active and ≥0.85, expect zero.
        proposals = await proposer.generate()
        assert proposals == []
