"""Tests for backend.knowledge.ingest.ingest_compiler — Lift E20 note schema.

E20 rewrites the system prompt to return Pattern/Principle/TechInsight/
DomainModel notes only. The schema gains an optional ``type`` field that
must be one of the four kinds; other values reject the action. Empty
arrays (``[]``) are a normal "no insight in this chunk" signal and must
NOT log as an error. Wikilinks are still validated against content as a
strict subset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter
from backend.knowledge.ingest.ingest_compiler import (
    BatchItem,
    IngestCompiler,
)
from backend.knowledge.ingest.ingest_compiler._actions import (
    NOTE_KIND_DOMAIN_MODEL,
    NOTE_KIND_PATTERN,
    NOTE_KIND_PRINCIPLE,
    NOTE_KIND_TECH_INSIGHT,
    VALID_NOTE_KINDS,
    validate_action,
)
from backend.knowledge.ingest.ingest_compiler._llm_compile import (
    COMPILE_BATCH_SYSTEM_PROMPT,
)


@pytest.fixture()
def vault_and_writer(tmp_path: Path) -> tuple[Vault, GardenWriter]:
    vault = Vault(tmp_path)
    vault.ensure_dirs()
    return vault, GardenWriter(vault)


@pytest.fixture()
def mock_llm() -> AsyncMock:
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value="[]")
    return llm


@pytest.fixture()
def mock_retriever() -> AsyncMock:
    retriever = AsyncMock()
    retriever.search = AsyncMock(return_value="No notes found.")
    return retriever


@pytest.fixture()
def make_compiler(
    vault_and_writer: tuple[Vault, GardenWriter],
    mock_llm: AsyncMock,
    mock_retriever: AsyncMock,
) -> Any:
    def _make(llm: AsyncMock | None = None) -> IngestCompiler:
        _, writer = vault_and_writer
        return IngestCompiler(
            garden_writer=writer,
            llm_client=llm if llm is not None else mock_llm,
            retriever=mock_retriever,
            event_bus=AsyncMock(),
            max_updates=10,
        )

    return _make


class TestValidNoteKinds:
    """The four note kinds are exposed as constants for the prompt + tests."""

    def test_valid_kind_set(self) -> None:
        assert VALID_NOTE_KINDS == {
            NOTE_KIND_PATTERN,
            NOTE_KIND_PRINCIPLE,
            NOTE_KIND_TECH_INSIGHT,
            NOTE_KIND_DOMAIN_MODEL,
        }
        assert "Pattern" in VALID_NOTE_KINDS

    def test_system_prompt_references_kinds(self) -> None:
        """The system prompt must literally mention the four kinds.

        This locks the prompt's contract against an accidental rename
        of a kind constant breaking the LLM's instructions silently.
        """
        for kind in VALID_NOTE_KINDS:
            assert kind in COMPILE_BATCH_SYSTEM_PROMPT, kind


class TestValidateActionE20Schema:
    """validate_action accepts the new ``type`` field, with constraints."""

    def _base(self, **overrides: Any) -> dict[str, Any]:
        action = {
            "action": "create",
            "title": "T",
            "content": "C",
            "reason": "R",
        }
        action.update(overrides)
        return action

    def test_accepts_action_without_type(self) -> None:
        """Backwards compat — old vaults / settle path don't set type."""
        assert validate_action(self._base()) is True

    def test_accepts_action_with_pattern_type(self) -> None:
        assert validate_action(self._base(type="Pattern")) is True

    def test_accepts_action_with_principle_type(self) -> None:
        assert validate_action(self._base(type="Principle")) is True

    def test_accepts_action_with_techinsight_type(self) -> None:
        assert validate_action(self._base(type="TechInsight")) is True

    def test_accepts_action_with_domainmodel_type(self) -> None:
        assert validate_action(self._base(type="DomainModel")) is True

    def test_rejects_action_with_invalid_type(self) -> None:
        assert validate_action(self._base(type="WhateverInvented")) is False

    def test_rejects_action_with_lowercase_type(self) -> None:
        # Strict — no case folding. The prompt sets exact spelling.
        assert validate_action(self._base(type="pattern")) is False

    def test_rejects_action_with_non_string_type(self) -> None:
        assert validate_action(self._base(type=42)) is False


class TestEmptyArrayResponse:
    """An empty JSON array is a normal "no insight" signal, not an error."""

    @pytest.mark.asyncio
    async def test_empty_array_returns_zero_notes(
        self,
        make_compiler: Any,
    ) -> None:
        compiler = make_compiler()
        result = await compiler.compile_batch(
            items=[BatchItem(label="src", content="trivial CRUD code")],
            seed_source="test",
        )
        assert result.notes_created == 0
        assert result.notes_updated == 0
        assert result.chunk_failures == 0


class TestTypeFieldPersistedAsFrontmatter:
    """A planned ``type: Pattern`` action lands as ``type: Pattern`` in YAML.

    We hook the new ``type`` field onto ``GardenNote.note_type`` so the
    existing writer flow surfaces it in the note's frontmatter without
    schema churn.
    """

    @pytest.mark.asyncio
    async def test_type_field_persists_to_frontmatter(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        make_compiler: Any,
        mock_llm: AsyncMock,
    ) -> None:
        vault, _ = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "type": "Pattern",
                    "title": "Per-call AsyncSession to escape concurrent flush race",
                    "content": (
                        "[[SQLAlchemy]] AsyncSession is not concurrent-safe. "
                        "Use an async_sessionmaker factory per branch."
                    ),
                    "wikilinks": ["[[SQLAlchemy]]"],
                    "tags": ["async-concurrency", "database-session"],
                    "reason": "principle is repo-independent",
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = make_compiler(llm=mock_llm)

        result = await compiler.compile_batch(
            items=[BatchItem(label="src", content="some code")],
            seed_source="test",
        )
        assert result.notes_created == 1

        seedling_dir = vault.root / "garden" / "seedling"
        files = sorted(seedling_dir.glob("*.md"))
        assert files, "expected at least one note"
        body = files[0].read_text()
        assert "type: Pattern" in body
        # Wikilinks survived the strict-subset filter.
        assert "[[SQLAlchemy]]" in body


class TestE27TypeThreadedToCanonicalize:
    """Lift E27 — when the ingest plan stamps ``type: Pattern`` on the
    note, that kind MUST be threaded into ``canonicalize_tags`` (and on
    through ``resolve_and_canonicalize`` → E26 wire) so the auto-created
    concept inherits the kind in its frontmatter.

    Pre-E27 the ingest path called ``canonicalize_tags(...)`` without
    ``note_type``, so every ingest-auto-created concept landed untyped
    even when the seedling note that triggered it had a kind.

    Tests target ``canonicalize_tags`` directly with a recording stub —
    the wire is the surface that needs locking, not the full ingest
    pipeline (which already has integration coverage elsewhere).
    """

    @pytest.mark.asyncio
    async def test_canonicalize_tags_forwards_note_type(self) -> None:
        from backend.knowledge.ingest.ingest_compiler._actions import (
            canonicalize_tags,
        )

        captured: list[dict[str, Any]] = []

        class _RecordingCanon:
            async def resolve_and_canonicalize(self, raw_tag: str, **kwargs: Any) -> str | None:
                captured.append({"raw_tag": raw_tag, **kwargs})
                return raw_tag  # echo back so it makes it into the returned list

        out = await canonicalize_tags(
            _RecordingCanon(),  # type: ignore[arg-type]
            ["pipe-drain"],
            raw_source="ingest-compiler",
            note_type="Pattern",
        )
        assert out == ["pipe-drain"]
        assert len(captured) == 1
        assert captured[0]["note_type"] == "Pattern", (
            "E27 — canonicalize_tags must forward note_type so the "
            "create-concept action carries it into E26's write path"
        )

    @pytest.mark.asyncio
    async def test_canonicalize_tags_back_compat_omits_note_type(self) -> None:
        from backend.knowledge.ingest.ingest_compiler._actions import (
            canonicalize_tags,
        )

        captured: list[dict[str, Any]] = []

        class _RecordingCanon:
            async def resolve_and_canonicalize(self, raw_tag: str, **kwargs: Any) -> str | None:
                captured.append({"raw_tag": raw_tag, **kwargs})
                return raw_tag

        await canonicalize_tags(
            _RecordingCanon(),  # type: ignore[arg-type]
            ["tag"],
            raw_source="ingest-compiler",
        )
        # Pre-E27 callers (and untyped notes) pass note_type=None — the
        # downstream service ignores it.
        assert captured[0].get("note_type") is None


class TestWikilinksAreStrictSubset:
    """The ``wikilinks`` field accepts ONLY targets that appear in content."""

    @pytest.mark.asyncio
    async def test_invented_wikilink_dropped(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        make_compiler: Any,
        mock_llm: AsyncMock,
    ) -> None:
        # The LLM claims [[InventedTopic]] but the content has no such
        # wikilink — clean_entities must drop it.
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "type": "Principle",
                    "title": "Principle title",
                    "content": "A principle about [[Real]] systems.",
                    "wikilinks": ["[[Real]]", "[[InventedTopic]]"],
                    "tags": ["x"],
                    "reason": "test",
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = make_compiler(llm=mock_llm)

        result = await compiler.compile_batch(
            items=[BatchItem(label="src", content="x")],
            seed_source="test",
        )
        assert result.notes_created == 1
        # Inspect entities on the executed action.
        assert result.actions_taken
        ents = result.actions_taken[0].entities
        assert "[[Real]]" in ents
        assert "[[InventedTopic]]" not in ents


class TestBackwardsCompatLegacyEntitiesField:
    """The legacy ``entities`` field still works for the settle path.

    The settle pipeline still uses the old prompt + entity field — the
    refactor keeps that surface intact while adding the new ``wikilinks``
    field for the new compiler prompt.
    """

    @pytest.mark.asyncio
    async def test_legacy_entities_field_still_recognized(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        make_compiler: Any,
        mock_llm: AsyncMock,
    ) -> None:
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": "Legacy",
                    "content": "Notes about [[Foo]].",
                    "entities": ["[[Foo]]"],
                    "tags": ["x"],
                    "reason": "legacy path",
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = make_compiler(llm=mock_llm)
        result = await compiler.compile_batch(
            items=[BatchItem(label="src", content="x")],
            seed_source="test",
        )
        assert result.notes_created == 1
        assert "[[Foo]]" in result.actions_taken[0].entities


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
