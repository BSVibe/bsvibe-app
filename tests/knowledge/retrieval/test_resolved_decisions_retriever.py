"""B11b — Resolved-decision retrieval for cross-run reuse.

Resolved decisions absorbed into the vault (via the ``decision_resolution``
settle path) must surface from the SAME workspace retriever the verifier + B6
seed already consult. The :class:`KnowledgeFactory.retriever()` becomes a
composite that returns canonical patterns AND relevant resolved decisions
(deduped, capped, workspace-scoped, graceful-empty).

These tests pin the composite contract at the boundary the rest of the system
sees — they do NOT call the resolve HTTP endpoint (that's covered by
``tests/api/test_checkpoints_settle.py``); they seed the same vault-side state
the settle absorber produces and assert it shows up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.execution.verifier.service import CanonRetriever
from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


async def _seed_resolved_decision_note(
    vault_root: Path,
    *,
    region: str,
    workspace_id: str,
    question: str,
    answer: str,
    intent_text: str | None = None,
) -> None:
    """Write a garden note shaped exactly like the settle sink writes one for a
    decision resolution, so the retriever sees real on-disk state."""
    ws_root = vault_root / region / workspace_id
    ws_root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(ws_root))
    summary = f"Decision resolved — Q: {question} A: {answer}"
    note = GardenNote(
        title=f"Settle: {summary[:80]}",
        content=summary,
        source="settle_worker",
        knowledge_layer="episodic",
        tags=["settle", "verified-run", "decision-resolution"],
        extra_fields={
            "kind": "decision_resolution",
            "question": question,
            "answer": answer,
            "intent_text": intent_text,
            "resolved_at": datetime.now(tz=UTC).isoformat(),
        },
    )
    await writer.write_garden(note)


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_id() -> str:
    return str(uuid.uuid4())


async def test_retriever_still_satisfies_canon_retriever_protocol(
    vault_root: Path, workspace_id: str
) -> None:
    """The composite retriever must still satisfy the CanonRetriever protocol so
    every existing caller (verifier, B6 seed, knowledge_search) keeps working."""
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert isinstance(factory.retriever(), CanonRetriever)


async def test_empty_workspace_returns_empty(vault_root: Path, workspace_id: str) -> None:
    """No decisions + no canon → ``[]`` (graceful-empty invariant preserved)."""
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert await factory.retriever().retrieve_for_signals("anything\nsrc/x.py") == []


async def test_resolved_decision_surfaces_for_matching_signal(
    vault_root: Path, workspace_id: str
) -> None:
    """A prior resolved decision whose intent/question/answer text overlaps the
    incoming signals shows up in the retriever output."""
    await _seed_resolved_decision_note(
        vault_root,
        region=_REGION,
        workspace_id=workspace_id,
        question="Which database should I target?",
        answer="Use Postgres",
        intent_text="pick a database",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals(
        "the user wants to pick a database for the new service"
    )
    joined = "\n".join(statements)
    assert "Postgres" in joined or "database" in joined.lower()
    # The retriever must surface BOTH the question and the answer (otherwise the
    # future run sees the topic but not the resolution it should reuse).
    assert any("Postgres" in s for s in statements), statements


async def test_resolved_decision_workspace_scoped(vault_root: Path, workspace_id: str) -> None:
    """A resolved decision in workspace A is invisible to workspace B."""
    other_workspace = str(uuid.uuid4())
    await _seed_resolved_decision_note(
        vault_root,
        region=_REGION,
        workspace_id=other_workspace,
        question="Which database?",
        answer="Use Postgres",
        intent_text="pick a database",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert await factory.retriever().retrieve_for_signals("pick a database") == []


async def test_resolved_decision_irrelevant_signal_no_surface(
    vault_root: Path, workspace_id: str
) -> None:
    """An unrelated signal does NOT pull in the resolved decision (no
    workspace-wide token dump — the retriever must filter by signal overlap)."""
    await _seed_resolved_decision_note(
        vault_root,
        region=_REGION,
        workspace_id=workspace_id,
        question="Which database should I target?",
        answer="Use Postgres",
        intent_text="pick a database",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals(
        "rotate the access logs on the nginx box"
    )
    # No overlap at all → empty (or at least no Postgres line surfaces).
    assert not any("Postgres" in s for s in statements), statements


async def test_resolved_decision_never_raises(vault_root: Path, workspace_id: str) -> None:
    """A malformed (empty / missing-frontmatter) decision note must NOT break
    retrieve — the verify path must never crash because knowledge was corrupt."""
    ws_root = vault_root / _REGION / workspace_id
    (ws_root / "garden" / "seedling").mkdir(parents=True, exist_ok=True)
    # A junk file under garden/seedling — same dir the settle sink writes into.
    (ws_root / "garden" / "seedling" / "junk.md").write_text("not yaml at all", encoding="utf-8")
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    # Never raises; returns at worst the canon part (here empty) gracefully.
    assert await factory.retriever().retrieve_for_signals("pick a database") == []


async def test_resolved_decision_cap(vault_root: Path, workspace_id: str) -> None:
    """The retriever caps decision results so a future run can't be flooded."""
    storage = FileSystemStorage(vault_root / _REGION / workspace_id)
    # Seed many on-topic decisions.
    for i in range(20):
        await _seed_resolved_decision_note(
            vault_root,
            region=_REGION,
            workspace_id=workspace_id,
            question=f"Database question {i}?",
            answer=f"Postgres-{i}",
            intent_text="pick a database",
        )
    assert len(await storage.list_files("garden/seedling")) == 20
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals("pick a database")
    # Cap is conservative — never the full 20.
    assert 0 < len(statements) <= 10
