"""Tests for CreateDecision action (Handoff §7.8)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.decisions import DecisionMemory
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


class TestCreateDecisionDraft:
    @pytest.mark.asyncio
    async def test_draft_path_under_decision_kind_dir(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": "decisions/cannot-link/20260507-140000-ci-cd.md",
                "subjects": ["ci", "cd"],
                "base_confidence": 0.95,
                "maturity": "seedling",
            },
        )
        # Per §7.8 — `actions/create-decision/<decision-kind>/<filename>`
        assert path.startswith("actions/create-decision/cannot-link/")
        assert await storage.exists(path)


class TestCreateDecisionValidation:
    @pytest.mark.asyncio
    async def test_invalid_decision_path_kind(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Bypass create_action_draft (which raises on slug derivation) and
        # write a malformed action directly to test the validate stage.
        store = service._store
        bad_path = "actions/create-decision/cannot-link/20260507-140000-x.md"
        await store.write_action(
            models.ActionEntry(
                path=bad_path,
                kind="create-decision",
                status="draft",
                action_schema_version="create-decision-v1",
                params={
                    "decision_path": "decisions/not-a-kind/20260507-140000-x.md",
                    "subjects": ["a", "b"],
                    "base_confidence": 0.9,
                    "maturity": "seedling",
                },
                created_at=datetime(2026, 5, 7),
                updated_at=datetime(2026, 5, 7),
                expires_at=datetime(2026, 5, 8),
            )
        )
        result = await service.apply_action(bad_path, actor="cli")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_empty_subjects_blocked(self, service: CanonicalizationService) -> None:
        path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": "decisions/cannot-link/20260507-140000-x.md",
                "subjects": [],
                "base_confidence": 0.9,
                "maturity": "seedling",
            },
        )
        result = await service.apply_action(path, actor="cli")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_base_confidence_out_of_range_blocked(
        self, service: CanonicalizationService
    ) -> None:
        path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": "decisions/cannot-link/20260507-140000-x.md",
                "subjects": ["a", "b"],
                "base_confidence": 1.5,
                "maturity": "seedling",
            },
        )
        result = await service.apply_action(path, actor="cli")
        assert result.final_status == "blocked"

    @pytest.mark.asyncio
    async def test_invalid_maturity_blocked(self, service: CanonicalizationService) -> None:
        path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": "decisions/cannot-link/20260507-140000-x.md",
                "subjects": ["a", "b"],
                "base_confidence": 0.9,
                "maturity": "ripe",
            },
        )
        result = await service.apply_action(path, actor="cli")
        assert result.final_status == "blocked"


class TestCreateDecisionEffects:
    @pytest.mark.asyncio
    async def test_decision_note_created(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        decision_path = "decisions/cannot-link/20260507-140000-ci-cd.md"
        action_path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": decision_path,
                "subjects": ["ci", "cd"],
                "base_confidence": 0.95,
                "maturity": "seedling",
            },
        )
        result = await service.apply_action(action_path, actor="cli")
        assert result.final_status == "applied"
        assert decision_path in result.affected_paths

        raw = await storage.read(decision_path)
        fm = extract_frontmatter(raw)
        assert fm["status"] == "active"
        assert fm["subjects"] == ["ci", "cd"]
        assert fm["base_confidence"] == 0.95
        # Default decay profile for cannot-link is definitional
        assert fm["decay"]["profile"] == "definitional"
        assert fm["decay"]["halflife_days"] is None
        assert fm["source_action"] == action_path

    @pytest.mark.asyncio
    async def test_decision_visible_in_memory_after_apply(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        decision_path = "decisions/cannot-link/20260507-140000-ci-cd.md"
        action_path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": decision_path,
                "subjects": ["ci", "cd"],
                "base_confidence": 0.95,
                "maturity": "seedling",
            },
        )
        await service.apply_action(action_path, actor="cli")

        memory = DecisionMemory(index=service._index, store=service._store)
        result = await memory.find_cannot_link(("ci", "cd"))
        assert len(result) == 1
        assert result[0].subjects == ("ci", "cd")
        assert result[0].source_action == action_path

    @pytest.mark.asyncio
    async def test_supersede_marks_old_decision(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        # Setup: existing active decision
        store = service._store
        old_path = "decisions/cannot-link/20260401-ci-cd.md"
        await store.write_decision(
            models.DecisionEntry(
                path=old_path,
                kind="cannot-link",
                status="active",
                maturity="seedling",
                decision_schema_version="cannot-link-v1",
                subjects=("ci", "cd"),
                base_confidence=0.5,
                last_confirmed_at=datetime(2026, 4, 1),
                decay_profile="definitional",
                decay_halflife_days=None,
                valid_from=datetime(2026, 4, 1),
                created_at=datetime(2026, 4, 1),
                updated_at=datetime(2026, 4, 1),
            )
        )
        await service._index.invalidate(old_path)

        # New CreateDecision supersedes the old one
        new_path = "decisions/cannot-link/20260507-140000-ci-cd.md"
        action_path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": new_path,
                "subjects": ["ci", "cd"],
                "base_confidence": 0.95,
                "maturity": "seedling",
                "supersedes": [old_path],
            },
        )
        result = await service.apply_action(action_path, actor="cli")
        assert result.final_status == "applied"
        # Per §7.8 — both paths in affected_paths
        assert new_path in result.affected_paths
        assert old_path in result.affected_paths

        # Old decision flipped to superseded
        old_fm = extract_frontmatter(await storage.read(old_path))
        assert old_fm["status"] == "superseded"
        assert old_fm["superseded_by"] == new_path

        # New decision links back
        new_fm = extract_frontmatter(await storage.read(new_path))
        assert old_path in new_fm["supersedes"]


class TestMustLinkSupport:
    @pytest.mark.asyncio
    async def test_must_link_decision(
        self, service: CanonicalizationService, storage: FileSystemStorage
    ) -> None:
        decision_path = "decisions/must-link/20260507-140000-auth.md"
        action_path = await service.create_action_draft(
            kind="create-decision",
            params={
                "decision_path": decision_path,
                "subjects": ["auth", "authn"],
                "base_confidence": 0.85,
                "maturity": "seedling",
                "decay_profile": "semantic",
            },
        )
        result = await service.apply_action(action_path, actor="cli")
        assert result.final_status == "applied"
        fm = extract_frontmatter(await storage.read(decision_path))
        assert fm["decay"]["profile"] == "semantic"
