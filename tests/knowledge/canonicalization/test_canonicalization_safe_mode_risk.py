"""Risk-aware Safe Mode gate (the corrected Safe Mode criterion).

Safe Mode must engage for genuinely *risky* canonicalization — knowledge
conflicts (a merge contradicting a ``cannot-link`` decision), oversized blast
radius, or action kinds outside the auto-apply allow-list — NOT for every
routine knowledge addition. Adding a fresh, non-conflicting concept is the
common case and should auto-apply even under Safe Mode.

The risk model already lived in the policy (``merge-auto-apply``'s
``safe_mode_on`` block: ``auto_apply_threshold`` / ``auto_action_kinds`` /
``max_affected_paths``) and the scorer (``stability_score`` knocked down by each
deterministic ``risk_reason``). These tests pin the *gate* that finally consults
them, plus the conservative fallback when no scorer/policy is wired.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.decisions import DecisionMemory
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.policies import PolicyResolver
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

_FIXED_NOW = datetime(2026, 5, 24, 12, 0, 0)


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(tmp_path)


async def _wired_service(storage: FileSystemStorage, *, safe_mode: bool) -> CanonicalizationService:
    """A fully-wired service (policies + decisions → scorer present), exactly
    like the production promoter path after Lift B."""
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    store = NoteStore(storage)
    policies = PolicyResolver(index=index, store=store, clock=lambda: _FIXED_NOW)
    await policies.bootstrap_defaults()
    decisions = DecisionMemory(index=index, store=store)
    return CanonicalizationService(
        store=store,
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        decisions=decisions,
        policies=policies,
        clock=lambda: _FIXED_NOW,
        safe_mode=lambda: safe_mode,
    )


class TestRiskAwareSafeModeGate:
    @pytest.mark.asyncio
    async def test_safe_mode_auto_applies_low_risk_create_concept(
        self, storage: FileSystemStorage
    ) -> None:
        """A clean create-concept (no conflict, score 1.0, kind allow-listed)
        auto-applies even under Safe Mode — adding knowledge is not 'risky'."""
        service = await _wired_service(storage, safe_mode=True)
        path = await service.create_action_draft(
            kind="create-concept",
            params={"concept": "python", "title": "Python"},
        )
        result = await service.apply_action(path, actor="promotion")

        assert result.final_status == "applied", result
        assert await storage.exists("concepts/active/python.md")
        # Audit trail records it auto-applied while Safe Mode was on.
        fm_action = await storage.read(path)
        assert "safe_mode: true" in fm_action.lower()

    @pytest.mark.asyncio
    async def test_safe_mode_queues_merge_conflicting_with_cannot_link(
        self, storage: FileSystemStorage
    ) -> None:
        """A merge that contradicts an active cannot-link decision (review band:
        below hard-block 0.85, at/above review 0.60) is genuinely risky → it is
        QUEUED for approval, not auto-applied."""
        service = await _wired_service(storage, safe_mode=False)
        # Seed two active concepts under permissive mode (prior approval).
        for cid in ("ci", "cd"):
            draft = await service.create_action_draft(
                kind="create-concept", params={"concept": cid, "title": cid}
            )
            assert (await service.apply_action(draft, actor="t")).final_status == "applied"

        # A cannot-link decision between them, strength 0.70 (review-warning band).
        decision = models.DecisionEntry(
            path="decisions/cannot-link/20260524-120000-ci-cd.md",
            kind="cannot-link",
            status="active",
            maturity="seedling",
            decision_schema_version="cannot-link-v1",
            subjects=("ci", "cd"),
            base_confidence=0.70,
            last_confirmed_at=_FIXED_NOW,
            decay_profile="definitional",
            decay_halflife_days=None,
            valid_from=_FIXED_NOW,
            created_at=_FIXED_NOW,
            updated_at=_FIXED_NOW,
        )
        await service._store.write_decision(decision)
        await service._index.invalidate(decision.path)

        # Now flip to Safe Mode and attempt the conflicting merge.
        service._safe_mode = lambda: True  # noqa: SLF001 — exercising the gate
        merge_draft = await service.create_action_draft(
            kind="merge-concepts", params={"canonical": "ci", "merge": ["cd"]}
        )
        result = await service.apply_action(merge_draft, actor="promotion")

        assert result.final_status == "pending_approval", result
        # Nothing merged — both concepts remain independent.
        assert await storage.exists("concepts/active/ci.md")
        assert await storage.exists("concepts/active/cd.md")
        assert not await storage.exists("concepts/merged/cd.md")

    @pytest.mark.asyncio
    async def test_safe_mode_auto_applies_clean_merge(self, storage: FileSystemStorage) -> None:
        """A merge with NO conflicting decision (score 1.0) auto-applies under
        Safe Mode — proving the queue in the conflict test is the conflict's
        doing, not a blanket 'all merges are risky' rule."""
        service = await _wired_service(storage, safe_mode=False)
        for cid in ("self-hosting", "self-host"):
            draft = await service.create_action_draft(
                kind="create-concept", params={"concept": cid, "title": cid}
            )
            assert (await service.apply_action(draft, actor="t")).final_status == "applied"

        service._safe_mode = lambda: True  # noqa: SLF001 — exercising the gate
        merge_draft = await service.create_action_draft(
            kind="merge-concepts",
            params={"canonical": "self-hosting", "merge": ["self-host"]},
        )
        result = await service.apply_action(merge_draft, actor="promotion")

        assert result.final_status == "applied", result
        assert await storage.exists("concepts/merged/self-host.md")

    @pytest.mark.asyncio
    async def test_safe_mode_without_scorer_queues_conservatively(
        self, storage: FileSystemStorage
    ) -> None:
        """No policy/decisions wired → no risk signal → conservative: queue.

        This preserves the original behaviour for callers that have not opted
        into scoring (the prior promotion-e2e default-policy contract)."""
        index = InMemoryCanonicalizationIndex()
        await index.initialize(storage)
        service = CanonicalizationService(
            store=NoteStore(storage),
            lock=AsyncIOMutationLock(),
            index=index,
            resolver=TagResolver(index=index),
            clock=lambda: _FIXED_NOW,
            safe_mode=lambda: True,
        )
        path = await service.create_action_draft(
            kind="create-concept", params={"concept": "python", "title": "Python"}
        )
        result = await service.apply_action(path, actor="promotion")

        assert result.final_status == "pending_approval", result
        assert not await storage.exists("concepts/active/python.md")

    @pytest.mark.asyncio
    async def test_safe_mode_off_auto_applies_as_before(self, storage: FileSystemStorage) -> None:
        """Sanity: with Safe Mode off, create-concept still auto-applies."""
        service = await _wired_service(storage, safe_mode=False)
        path = await service.create_action_draft(
            kind="create-concept", params={"concept": "python", "title": "Python"}
        )
        result = await service.apply_action(path, actor="promotion")
        assert result.final_status == "applied", result
        assert await storage.exists("concepts/active/python.md")
