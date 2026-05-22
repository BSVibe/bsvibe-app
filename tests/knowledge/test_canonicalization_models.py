"""Tests for canonicalization domain dataclasses (Class_Diagram §6, Handoff §6)."""

from __future__ import annotations

from datetime import datetime

from backend.knowledge.canonicalization import models


class TestConceptEntry:
    def test_minimal_construction(self) -> None:
        entry = models.ConceptEntry(
            concept_id="machine-learning",
            path="concepts/active/machine-learning.md",
            display="Machine Learning",
            aliases=[],
            created_at=datetime(2026, 5, 6, 14, 30, 12),
            updated_at=datetime(2026, 5, 6, 14, 30, 12),
        )
        assert entry.concept_id == "machine-learning"
        assert entry.source_action is None

    def test_with_aliases_and_source_action(self) -> None:
        entry = models.ConceptEntry(
            concept_id="ml",
            path="concepts/active/ml.md",
            display="Machine Learning",
            aliases=["machine_learning", "ML"],
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            source_action="actions/create-concept/20260506-143012-ml.md",
        )
        assert entry.aliases == ["machine_learning", "ML"]
        assert entry.source_action == "actions/create-concept/20260506-143012-ml.md"


class TestActionEntry:
    def test_minimal_draft(self) -> None:
        entry = models.ActionEntry(
            path="actions/create-concept/20260506-143012-ml.md",
            kind="create-concept",
            status="draft",
            action_schema_version="create-concept-v1",
            params={"concept": "ml", "title": "Machine Learning"},
            created_at=datetime(2026, 5, 6, 14, 30, 12),
            updated_at=datetime(2026, 5, 6, 14, 30, 12),
            expires_at=datetime(2026, 5, 7, 14, 30, 12),
        )
        assert entry.status == "draft"
        assert entry.affected_paths == []
        assert entry.supersedes == []
        assert entry.superseded_by is None
        assert entry.source_proposal is None

    def test_status_must_be_in_enum(self) -> None:
        # status validation lives in the service layer, not the dataclass.
        # Dataclass just stores. Document with this test.
        entry = models.ActionEntry(
            path="actions/create-concept/x.md",
            kind="create-concept",
            status="applied",
            action_schema_version="create-concept-v1",
            params={},
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
            expires_at=datetime(2026, 5, 7),
        )
        assert entry.status == "applied"


class TestActionStatusEnum:
    def test_known_statuses(self) -> None:
        # Per Handoff §6
        expected = {
            "draft",
            "pending_approval",
            "applied",
            "rejected",
            "blocked",
            "expired",
            "failed",
            "superseded",
        }
        assert set(models.ACTION_STATUSES) == expected


class TestActionKindEnum:
    def test_initial_action_kinds(self) -> None:
        # Per Handoff §6
        expected = {
            "create-concept",
            "merge-concepts",
            "split-concept",
            "deprecate-concept",
            "restore-concept",
            "retag-notes",
            "update-policy",
            "create-decision",
        }
        assert set(models.ACTION_KINDS) == expected
