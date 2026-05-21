"""Tests for canonicalization paths and id helpers (Handoff §1, §2)."""

from __future__ import annotations

import pytest

from backend.knowledge.canonicalization import paths


class TestConceptId:
    @pytest.mark.parametrize(
        "concept_id",
        [
            "machine-learning",
            "ml",
            "rag-evaluation",
            "self-hosting",
            "k8s",
            "x",
            "a1",
            "abc-123-def",
        ],
    )
    def test_valid_concept_ids(self, concept_id: str) -> None:
        assert paths.is_valid_concept_id(concept_id)

    @pytest.mark.parametrize(
        "concept_id",
        [
            "",
            "Machine-Learning",
            "machine_learning",
            "1abc",
            "-abc",
            "abc-",
            "abc--def",
            "abc.def",
            "abc/def",
            "..",
            "ABC",
        ],
    )
    def test_invalid_concept_ids(self, concept_id: str) -> None:
        assert not paths.is_valid_concept_id(concept_id)

    def test_validate_raises_on_invalid(self) -> None:
        with pytest.raises(ValueError, match="invalid concept id"):
            paths.validate_concept_id("Bad_ID")

    def test_validate_returns_id_on_valid(self) -> None:
        assert paths.validate_concept_id("machine-learning") == "machine-learning"


class TestActionTimestamp:
    def test_format_timestamp_pattern(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 12)
        assert paths.format_action_timestamp(dt) == "20260506-143012"


class TestActionFilename:
    def test_basic(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 12)
        result = paths.build_action_filename(dt, "machine-learning")
        assert result == "20260506-143012-machine-learning.md"

    def test_slug_must_be_valid(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 12)
        with pytest.raises(ValueError, match="invalid slug"):
            paths.build_action_filename(dt, "Bad_Slug")


class TestCollisionSuffix:
    def test_no_collision_returns_original(self) -> None:
        existing: set[str] = set()
        result = paths.with_collision_suffix(
            "actions/create-concept/20260506-143012-ml.md", existing
        )
        assert result == "actions/create-concept/20260506-143012-ml.md"

    def test_first_collision_appends_02(self) -> None:
        existing = {"actions/create-concept/20260506-143012-ml.md"}
        result = paths.with_collision_suffix(
            "actions/create-concept/20260506-143012-ml.md", existing
        )
        assert result == "actions/create-concept/20260506-143012-ml-02.md"

    def test_second_collision_appends_03(self) -> None:
        existing = {
            "actions/create-concept/20260506-143012-ml.md",
            "actions/create-concept/20260506-143012-ml-02.md",
        }
        result = paths.with_collision_suffix(
            "actions/create-concept/20260506-143012-ml.md", existing
        )
        assert result == "actions/create-concept/20260506-143012-ml-03.md"


class TestActionPath:
    def test_concept_action_path(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 12)
        result = paths.build_action_path("create-concept", dt, "machine-learning")
        assert result == "actions/create-concept/20260506-143012-machine-learning.md"

    def test_retag_action_path(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 55)
        result = paths.build_action_path("retag-notes", dt, "foo")
        assert result == "actions/retag-notes/20260506-143055-foo.md"

    def test_invalid_action_kind(self) -> None:
        from datetime import datetime

        dt = datetime(2026, 5, 6, 14, 30, 12)
        with pytest.raises(ValueError, match="unknown action kind"):
            paths.build_action_path("not-a-kind", dt, "ml")


class TestConceptPath:
    def test_active_concept_path(self) -> None:
        result = paths.active_concept_path("machine-learning")
        assert result == "concepts/active/machine-learning.md"

    def test_active_concept_path_validates(self) -> None:
        with pytest.raises(ValueError, match="invalid concept id"):
            paths.active_concept_path("Bad_ID")
