"""DecisionNoteLocator — map a folded decision/rejection statement back to the
garden note it came from, so the delivery report can LINK to the stored
knowledge instead of rendering a dead English tag.

The locator must reconstruct the SAME statement string the
ResolvedDecisionsRetriever / NegativePatternRetriever fold into the verify
contract, so an exact match resolves the note path. These tests seed the real
on-disk garden state the settle sink writes and assert the map.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter
from backend.knowledge.retrieval.decision_note_locator import DecisionNoteLocator

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


async def _seed_note(
    ws_root: Path,
    *,
    kind: str,
    question: str | None = None,
    answer: str | None = None,
    reason: str | None = None,
    retracted_at: str | None = None,
) -> None:
    ws_root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(ws_root))
    extra: dict[str, object | None] = {"kind": kind}
    if question is not None:
        extra["question"] = question
    if answer is not None:
        extra["answer"] = answer
    if reason is not None:
        extra["reason"] = reason
    if retracted_at is not None:
        extra["retracted_at"] = retracted_at
    note = GardenNote(
        title=f"Settle: {kind}",
        content="body",
        source="settle_worker",
        knowledge_layer="episodic",
        tags=["settle"],
        extra_fields=extra,
    )
    await writer.write_garden(note)


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    return tmp_path / "vault" / _REGION / str(uuid.uuid4())


async def test_empty_vault_returns_empty(ws_root: Path) -> None:
    ws_root.mkdir(parents=True, exist_ok=True)
    locator = DecisionNoteLocator(FileSystemStorage(ws_root))
    assert await locator.statement_paths() == {}


async def test_decision_statement_maps_to_its_note_path(ws_root: Path) -> None:
    await _seed_note(
        ws_root, kind="decision_resolution", question="Which database?", answer="Use Postgres"
    )
    locator = DecisionNoteLocator(FileSystemStorage(ws_root))
    paths = await locator.statement_paths()
    # The key is the EXACT statement the ResolvedDecisionsRetriever folds in.
    assert "Prior decision — Q: Which database? A: Use Postgres" in paths
    path = paths["Prior decision — Q: Which database? A: Use Postgres"]
    assert path.startswith("garden/seedling/") and path.endswith(".md")


async def test_rejection_statement_maps_to_its_note_path(ws_root: Path) -> None:
    await _seed_note(
        ws_root, kind="negative_pattern", reason="never ship without a regression test"
    )
    locator = DecisionNoteLocator(FileSystemStorage(ws_root))
    paths = await locator.statement_paths()
    assert "Avoid (prior rejection) — never ship without a regression test" in paths


async def test_retracted_note_is_skipped(ws_root: Path) -> None:
    await _seed_note(
        ws_root,
        kind="decision_resolution",
        question="Which cache?",
        answer="Redis",
        retracted_at=datetime.now(tz=UTC).isoformat(),
    )
    locator = DecisionNoteLocator(FileSystemStorage(ws_root))
    assert await locator.statement_paths() == {}


async def test_non_decision_note_is_ignored(ws_root: Path) -> None:
    # A plain observation note (no decision/rejection kind) contributes nothing.
    await _seed_note(ws_root, kind="observation")
    locator = DecisionNoteLocator(FileSystemStorage(ws_root))
    assert await locator.statement_paths() == {}
