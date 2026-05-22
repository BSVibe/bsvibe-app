"""Direct unit tests for ``backend.knowledge.graph.note`` (M15 split).

These tests exercise the pure-data layer (``GardenNote``, ``slugify``,
``build_frontmatter``, constants) in isolation so future refactoring of
``writer_core.py`` cannot silently break the contract that downstream code
depends on.
"""

from __future__ import annotations

import re

import pytest
import yaml

from backend.knowledge.graph.note import (
    _MAX_ACTION_SUMMARY,
    GardenNote,
    _build_frontmatter,
    _slugify,
    build_frontmatter,
    slugify,
)


class TestGardenNoteDataclass:
    def test_minimum_fields(self) -> None:
        note = GardenNote(title="t", content="c", source="src")
        assert note.title == "t"
        assert note.content == "c"
        assert note.source == "src"
        # note_type is now an opt-in compatibility field — defaults to None
        # since identity comes from tags + entities + community.
        assert note.note_type is None

    def test_note_type_optional_for_backwards_compat(self) -> None:
        # Reading a vault written before the dynamic-ontology refactor
        # must not crash — the field still accepts the old enum values.
        note = GardenNote(title="t", content="c", source="src", note_type="idea")
        assert note.note_type == "idea"

    def test_collection_defaults_are_independent(self) -> None:
        a = GardenNote(title="a", content="", source="src")
        b = GardenNote(title="b", content="", source="src")
        a.tags.append("x")
        a.entities.append("[[E]]")
        a.related.append("y")
        a.relations.setdefault("attendees", []).append("z")
        a.aliases.append("alpha")
        a.extra_fields["k"] = "v"
        # b must not share state (default_factory invariant)
        assert b.tags == []
        assert b.entities == []
        assert b.related == []
        assert b.relations == {}
        assert b.aliases == []
        assert b.extra_fields == {}

    def test_default_confidence_and_layer(self) -> None:
        note = GardenNote(title="t", content="c", source="s")
        assert note.confidence == pytest.approx(0.9)
        assert note.knowledge_layer == "semantic"


class TestSlugify:
    def test_lowercase_and_dash(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_collapses_spaces(self) -> None:
        assert slugify("a    b   c") == "a-b-c"

    def test_strips_special_characters(self) -> None:
        assert slugify("Hello, World!") == "hello-world"

    def test_preserves_unicode_word_chars(self) -> None:
        # Korean characters must survive
        assert "한글" in slugify("한글 노트")

    def test_falls_back_to_timestamp_when_empty(self) -> None:
        result = slugify("!!!")
        assert re.fullmatch(r"\d{8}-\d{6}", result)

    def test_legacy_alias(self) -> None:
        # Underscore-prefixed alias kept for backwards compatibility.
        assert _slugify is slugify


class TestBuildFrontmatter:
    def test_returns_yaml_block(self) -> None:
        out = build_frontmatter({"type": "idea", "tags": ["a", "b"]})
        assert out.startswith("---\n")
        assert out.endswith("\n---\n")
        body = out[4:-5]
        loaded = yaml.safe_load(body)
        assert loaded == {"type": "idea", "tags": ["a", "b"]}

    def test_unicode_round_trip(self) -> None:
        out = build_frontmatter({"title": "한글 제목"})
        assert "한글 제목" in out
        loaded = yaml.safe_load(out.strip("-\n"))
        assert loaded == {"title": "한글 제목"}

    def test_legacy_alias(self) -> None:
        assert _build_frontmatter is build_frontmatter


class TestActionSummaryCap:
    def test_constant_is_positive(self) -> None:
        # Used by GardenWriter.write_action — must be positive int.
        assert isinstance(_MAX_ACTION_SUMMARY, int)
        assert _MAX_ACTION_SUMMARY > 0
