"""Tests for DecisionEntry + PolicyEntry + NoteStore CRUD (Handoff §8)."""

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


class TestDecisionPaths:
    def test_decision_path_cannot_link(self) -> None:
        dt = datetime(2026, 5, 6, 16, 0, 0)
        result = paths.build_decision_path("cannot-link", dt, "ci-cd")
        assert result == "decisions/cannot-link/20260506-160000-ci-cd.md"

    def test_decision_path_must_link(self) -> None:
        dt = datetime(2026, 5, 6, 16, 0, 0)
        result = paths.build_decision_path("must-link", dt, "auth-authn")
        assert result == "decisions/must-link/20260506-160000-auth-authn.md"

    def test_invalid_decision_kind(self) -> None:
        dt = datetime(2026, 5, 6, 16, 0, 0)
        with pytest.raises(ValueError, match="unknown decision kind"):
            paths.build_decision_path("not-a-kind", dt, "x")

    def test_create_decision_action_path(self) -> None:
        dt = datetime(2026, 5, 6, 16, 0, 0)
        result = paths.build_create_decision_action_path("cannot-link", dt, "ci-cd")
        assert result == "actions/create-decision/cannot-link/20260506-160000-ci-cd.md"


class TestPolicyPaths:
    def test_policy_path(self) -> None:
        result = paths.build_policy_path("staleness", "conservative-default")
        assert result == "decisions/policy/staleness/conservative-default.md"

    def test_invalid_policy_kind(self) -> None:
        with pytest.raises(ValueError, match="unknown policy kind"):
            paths.build_policy_path("nope", "x")

    def test_known_policy_kinds(self) -> None:
        # Per Handoff §8.2
        expected = {"staleness", "merge-auto-apply", "decision-maturity"}
        assert set(paths.POLICY_KINDS) == expected


class TestDecisionEntryShape:
    def test_minimal_construction(self) -> None:
        entry = models.DecisionEntry(
            path="decisions/cannot-link/20260506-160000-ci-cd.md",
            kind="cannot-link",
            status="active",
            maturity="seedling",
            decision_schema_version="cannot-link-v1",
            subjects=("ci", "cd"),
            base_confidence=0.95,
            last_confirmed_at=datetime(2026, 5, 6),
            decay_profile="definitional",
            decay_halflife_days=None,
            valid_from=datetime(2026, 5, 6),
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        assert entry.subjects == ("ci", "cd")
        assert entry.supersedes == []
        assert entry.superseded_by is None


class TestPolicyEntryShape:
    def test_minimal_construction(self) -> None:
        entry = models.PolicyEntry(
            path="decisions/policy/staleness/conservative-default.md",
            kind="staleness",
            status="active",
            profile_name="conservative-default",
            priority=100,
            scope={},
            policy_schema_version="staleness-policy-v1",
            valid_from=datetime(2026, 5, 6),
            params={},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        assert entry.expires_at is None


class TestReadWriteDecision:
    @pytest.mark.asyncio
    async def test_round_trip_cannot_link(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        entry = models.DecisionEntry(
            path="decisions/cannot-link/20260506-160000-ci-cd.md",
            kind="cannot-link",
            status="active",
            maturity="seedling",
            decision_schema_version="cannot-link-v1",
            subjects=("ci", "cd"),
            base_confidence=0.95,
            last_confirmed_at=datetime(2026, 5, 6, 16, 0, 0),
            decay_profile="definitional",
            decay_halflife_days=None,
            valid_from=datetime(2026, 5, 6, 16, 0, 0),
            created_at=datetime(2026, 5, 6, 16, 0, 0),
            updated_at=datetime(2026, 5, 6, 16, 0, 0),
            source_action="actions/create-decision/cannot-link/20260506-160000-ci-cd.md",
        )
        await store.write_decision(entry)
        got = await store.read_decision(entry.path)
        assert got is not None
        assert got.kind == "cannot-link"
        assert got.status == "active"
        assert got.subjects == ("ci", "cd")
        assert got.base_confidence == 0.95
        assert got.decay_profile == "definitional"
        assert got.decay_halflife_days is None
        assert got.source_action.endswith("ci-cd.md")

    @pytest.mark.asyncio
    async def test_kind_path_derived_not_in_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        entry = models.DecisionEntry(
            path="decisions/cannot-link/x.md",
            kind="cannot-link",
            status="active",
            maturity="seedling",
            decision_schema_version="cannot-link-v1",
            subjects=("ci", "cd"),
            base_confidence=0.9,
            last_confirmed_at=datetime(2026, 5, 6),
            decay_profile="definitional",
            decay_halflife_days=None,
            valid_from=datetime(2026, 5, 6),
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        await store.write_decision(entry)
        raw = await storage.read(entry.path)
        fm = extract_frontmatter(raw)
        # Per Handoff §0.2 — decision_type forbidden
        assert "decision_type" not in fm

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, store: NoteStore) -> None:
        result = await store.read_decision("decisions/cannot-link/missing.md")
        assert result is None


class TestReadWritePolicy:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: NoteStore, storage: FileSystemStorage) -> None:
        entry = models.PolicyEntry(
            path="decisions/policy/merge-auto-apply/conservative-default.md",
            kind="merge-auto-apply",
            status="active",
            profile_name="conservative-default",
            priority=100,
            scope={"action_kinds": ["merge-concepts"]},
            policy_schema_version="merge-auto-apply-policy-v1",
            valid_from=datetime(2026, 5, 6),
            params={
                "safe_mode_on": {"auto_apply_threshold": 0.90},
                "hard_blocks": {"cannot_link_threshold": 0.85},
            },
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        await store.write_policy(entry)
        got = await store.read_policy(entry.path)
        assert got is not None
        assert got.kind == "merge-auto-apply"
        assert got.profile_name == "conservative-default"
        assert got.priority == 100
        assert got.scope == {"action_kinds": ["merge-concepts"]}
        assert got.params["hard_blocks"]["cannot_link_threshold"] == 0.85

    @pytest.mark.asyncio
    async def test_kind_path_derived_not_in_frontmatter(
        self, store: NoteStore, storage: FileSystemStorage
    ) -> None:
        entry = models.PolicyEntry(
            path="decisions/policy/staleness/conservative-default.md",
            kind="staleness",
            status="active",
            profile_name="conservative-default",
            priority=100,
            scope={},
            policy_schema_version="staleness-policy-v1",
            valid_from=datetime(2026, 5, 6),
            params={},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
        await store.write_policy(entry)
        raw = await storage.read(entry.path)
        fm = extract_frontmatter(raw)
        assert "policy_type" not in fm
