"""SkillMeta — validation per Workflow §6 #5."""

from __future__ import annotations

import pytest

from backend.extensions.skill.exceptions import SkillLoadError
from backend.extensions.skill.meta import (
    ALLOWED_FRONTMATTER_FIELDS,
    DROPPED_FRONTMATTER_FIELDS,
    REQUIRED_FRONTMATTER_FIELDS,
    SkillMeta,
)


def test_minimal_required_fields_only() -> None:
    m = SkillMeta(name="weekly-digest", version="1.0.0", description="X")
    assert m.name == "weekly-digest"
    assert m.author == ""
    assert m.allowed_tools == []
    assert m.model is None
    assert m.system_prompt == ""


def test_full_optional_fields() -> None:
    m = SkillMeta(
        name="x",
        version="2.0.0",
        description="desc",
        author="ada",
        allowed_tools=["search_knowledge", "create_note"],
        model="openai/gpt-4o",
        system_prompt="Do the thing.",
    )
    assert m.author == "ada"
    assert m.allowed_tools == ["search_knowledge", "create_note"]
    assert m.model == "openai/gpt-4o"


def test_rejects_invalid_name() -> None:
    with pytest.raises(SkillLoadError, match="Invalid skill name"):
        SkillMeta(name="Bad Name", version="1.0", description="x")


def test_rejects_empty_version() -> None:
    with pytest.raises(SkillLoadError, match="missing version"):
        SkillMeta(name="x", version="", description="d")


def test_rejects_empty_description() -> None:
    with pytest.raises(SkillLoadError, match="missing description"):
        SkillMeta(name="x", version="1.0", description="")


def test_frontmatter_field_sets_disjoint() -> None:
    # Workflow §6 #5: required + author/allowed_tools/model is the ALLOWED set;
    # category/trigger/etc are the DROPPED set. The two MUST be disjoint.
    assert REQUIRED_FRONTMATTER_FIELDS.isdisjoint(DROPPED_FRONTMATTER_FIELDS)
    assert ALLOWED_FRONTMATTER_FIELDS.isdisjoint(DROPPED_FRONTMATTER_FIELDS)
    assert REQUIRED_FRONTMATTER_FIELDS.issubset(ALLOWED_FRONTMATTER_FIELDS)


def test_dropped_fields_match_workflow_locked_set() -> None:
    # Frozen per Workflow §6 #5. Adding to this set must be a conscious decision.
    assert DROPPED_FRONTMATTER_FIELDS == frozenset(
        {
            "category",
            "trigger",
            "read_context",
            "output_target",
            "output_note_type",
            "output_format",
            "credentials",
        }
    )
