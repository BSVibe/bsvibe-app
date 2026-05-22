"""Tests for CanonicalizationScorer with envelope-shaped risk_reasons (§13)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.scoring import CanonicalizationScorer
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


@pytest.fixture
async def policies(index: InMemoryCanonicalizationIndex, store: NoteStore) -> PolicyResolver:
    pr = PolicyResolver(index=index, store=store, clock=lambda: datetime(2026, 5, 7, 14, 0, 0))
    await pr.bootstrap_defaults()
    return pr


@pytest.fixture
def decisions(index: InMemoryCanonicalizationIndex, store: NoteStore) -> DecisionMemory:
    return DecisionMemory(index=index, store=store)


@pytest.fixture
def scorer(decisions: DecisionMemory, policies: PolicyResolver) -> CanonicalizationScorer:
    return CanonicalizationScorer(
        decisions=decisions,
        policies=policies,
        clock=lambda: datetime(2026, 5, 7, 14, 0, 0),
    )


def _action(
    *,
    kind: str = "merge-concepts",
    params: dict | None = None,
    affected: int = 1,
) -> models.ActionEntry:
    entry = models.ActionEntry(
        path=f"actions/{kind}/x.md",
        kind=kind,
        status="draft",
        action_schema_version=f"{kind}-v1",
        params=params or {"canonical": "ci", "merge": ["cd"]},
        created_at=datetime(2026, 5, 7),
        updated_at=datetime(2026, 5, 7),
        expires_at=datetime(2026, 5, 8),
    )
    # Simulate a previously-computed affected_paths estimate
    entry.affected_paths = [f"path/{i}.md" for i in range(affected)]
    return entry


class TestScoreResultShape:
    @pytest.mark.asyncio
    async def test_envelope_fields_present(self, scorer: CanonicalizationScorer) -> None:
        action = _action()
        result = await scorer.score(action)
        # Per §13 — required fields
        assert isinstance(result.stability_score, float)
        assert isinstance(result.hard_blocks, list)
        assert isinstance(result.risk_reasons, list)
        assert isinstance(result.deterministic_evidence, list)
        assert isinstance(result.model_evidence, list)
        assert isinstance(result.human_evidence, list)
        assert result.scorer_version is not None
        assert result.policy_profile_path is not None

    @pytest.mark.asyncio
    async def test_clean_action_high_score(self, scorer: CanonicalizationScorer) -> None:
        action = _action(affected=2)
        result = await scorer.score(action)
        assert result.stability_score >= 0.85
        assert result.risk_reasons == []


class TestRiskReasonsEnvelopeShape:
    @pytest.mark.asyncio
    async def test_blast_radius_emitted_as_deterministic(
        self, scorer: CanonicalizationScorer
    ) -> None:
        # Default policy: max_affected_paths.merge-concepts = 10
        action = _action(affected=20)
        result = await scorer.score(action)

        blast = [r for r in result.risk_reasons if r["kind"] == "blast_radius_exceeded"]
        assert len(blast) == 1
        # Per §13 envelope shape — source MUST be deterministic (rule violation)
        assert blast[0]["source"] == "deterministic"
        assert "schema_version" in blast[0]
        assert "observed_at" in blast[0]
        assert "producer" in blast[0]
        assert blast[0]["payload"]["affected_count"] == 20
        assert blast[0]["payload"]["cap"] == 10
        # Same item also appears in deterministic_evidence (not model/human)
        det_kinds = [e["kind"] for e in result.deterministic_evidence]
        assert "blast_radius_exceeded" in det_kinds
        # Score gets penalized
        assert result.stability_score < 0.85

    @pytest.mark.asyncio
    async def test_weak_cannot_link_emits_deterministic_risk(
        self,
        scorer: CanonicalizationScorer,
        store: NoteStore,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        # Below hard_block threshold (0.85) but above review (0.6)
        d = models.DecisionEntry(
            path="decisions/cannot-link/20260507-140000-ci-cd.md",
            kind="cannot-link",
            status="active",
            maturity="seedling",
            decision_schema_version="cannot-link-v1",
            subjects=("ci", "cd"),
            base_confidence=0.70,
            last_confirmed_at=datetime(2026, 5, 7),
            decay_profile="definitional",
            decay_halflife_days=None,
            valid_from=datetime(2026, 5, 7),
            created_at=datetime(2026, 5, 7),
            updated_at=datetime(2026, 5, 7),
        )
        await store.write_decision(d)
        await index.invalidate(d.path)

        action = _action(params={"canonical": "ci", "merge": ["cd"]})
        result = await scorer.score(action)
        risks = [r for r in result.risk_reasons if r["kind"] == "prior_decision_conflict"]
        assert len(risks) == 1
        # Deterministic — derived from index lookup, not a model
        assert risks[0]["source"] == "deterministic"
        assert risks[0]["payload"]["effective_strength"] == 0.70
        assert risks[0]["payload"]["decision_path"] == d.path

    @pytest.mark.asyncio
    async def test_no_risks_for_unrelated_pair(
        self,
        scorer: CanonicalizationScorer,
        store: NoteStore,
        index: InMemoryCanonicalizationIndex,
    ) -> None:
        # Cannot-link on (ci, cd), but action merges (a, b)
        d = models.DecisionEntry(
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
        await store.write_decision(d)
        await index.invalidate(d.path)

        action = _action(params={"canonical": "a", "merge": ["b"]})
        result = await scorer.score(action)
        assert result.risk_reasons == []


class TestSourceSeparation:
    @pytest.mark.asyncio
    async def test_no_model_or_human_evidence_in_v1(self, scorer: CanonicalizationScorer) -> None:
        # Slice 4 scorer is deterministic-only (LLM-verify is layered later).
        action = _action(affected=20)
        result = await scorer.score(action)
        assert result.model_evidence == []
        assert result.human_evidence == []
