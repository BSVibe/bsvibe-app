"""G1 — Negative-pattern retrieval surfaces prior rejection feedback.

When the founder discards a deliverable with a *reason*, that reason is absorbed
into the workspace vault as a ``kind: negative_pattern`` garden note (the same
settle pipeline B11b uses for resolved decisions). This retriever reads that
state back and surfaces RELEVANT rejection feedback for an incoming change's
signals — so a future run's verify contract (B3 fold) and B6 knowledge seed
carry the founder's "don't do this again" guidance instead of repeating the
rejected approach.

These tests pin the composite contract at the boundary the rest of the system
sees: they seed the same vault-side state the settle absorber produces and
assert it shows up (the endpoint → absorb path is covered by
``tests/api/test_checkpoints_negative_pattern.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter
from backend.workflow.application.verification_service import CanonRetriever

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


async def _seed_negative_pattern_note(
    vault_root: Path,
    *,
    region: str,
    workspace_id: str,
    reason: str,
    question: str = "",
    intent_text: str | None = None,
) -> None:
    """Write a garden note shaped exactly like the settle sink writes one for a
    discard-with-reason, so the retriever sees real on-disk state."""
    ws_root = vault_root / region / workspace_id
    ws_root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(ws_root))
    summary = f"Rejected approach — {reason}"
    note = GardenNote(
        title=f"Settle: {summary[:80]}",
        content=summary,
        source="settle_worker",
        knowledge_layer="episodic",
        tags=["settle", "verified-run", "negative-pattern"],
        extra_fields={
            "kind": "negative_pattern",
            "reason": reason,
            "question": question,
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
    """Adding the negative-pattern source must keep the composite satisfying the
    CanonRetriever protocol so every existing caller keeps working."""
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert isinstance(factory.retriever(), CanonRetriever)


async def test_negative_pattern_surfaces_for_matching_signal(
    vault_root: Path, workspace_id: str
) -> None:
    """A prior rejection whose reason/intent overlaps the incoming signals shows
    up in the retriever output as an avoid-this statement."""
    await _seed_negative_pattern_note(
        vault_root,
        region=_REGION,
        workspace_id=workspace_id,
        reason="never ship a payment change without a regression test",
        intent_text="adjust the payment settings screen",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals(
        "the founder wants to change the payment settings screen layout"
    )
    joined = "\n".join(statements)
    assert "regression test" in joined.lower()
    # The reason must surface as guidance, not be silently swallowed.
    assert any("payment" in s.lower() for s in statements), statements


async def test_negative_pattern_workspace_scoped(vault_root: Path, workspace_id: str) -> None:
    """A rejection in workspace A is invisible to workspace B."""
    other_workspace = str(uuid.uuid4())
    await _seed_negative_pattern_note(
        vault_root,
        region=_REGION,
        workspace_id=other_workspace,
        reason="never ship a payment change without a regression test",
        intent_text="payment settings",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert await factory.retriever().retrieve_for_signals("payment settings") == []


async def test_negative_pattern_irrelevant_signal_no_surface(
    vault_root: Path, workspace_id: str
) -> None:
    """An unrelated signal does NOT pull in the rejection (no workspace-wide
    token dump — the retriever must filter by signal overlap)."""
    await _seed_negative_pattern_note(
        vault_root,
        region=_REGION,
        workspace_id=workspace_id,
        reason="never ship a payment change without a regression test",
        intent_text="payment settings",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals(
        "rotate the access logs on the nginx box"
    )
    assert not any("payment" in s.lower() for s in statements), statements


async def test_negative_pattern_never_raises(vault_root: Path, workspace_id: str) -> None:
    """A malformed negative-pattern note must NOT break retrieve — the verify
    path must never crash because knowledge was corrupt."""
    ws_root = vault_root / _REGION / workspace_id
    (ws_root / "garden" / "seedling").mkdir(parents=True, exist_ok=True)
    (ws_root / "garden" / "seedling" / "junk.md").write_text("not yaml at all", encoding="utf-8")
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    assert await factory.retriever().retrieve_for_signals("payment settings") == []


async def test_negative_pattern_cap(vault_root: Path, workspace_id: str) -> None:
    """The retriever caps rejection results so a future run can't be flooded."""
    storage = FileSystemStorage(vault_root / _REGION / workspace_id)
    for i in range(20):
        await _seed_negative_pattern_note(
            vault_root,
            region=_REGION,
            workspace_id=workspace_id,
            reason=f"payment rule {i} — always add a regression test",
            intent_text="payment settings",
        )
    assert len(await storage.list_files("garden/seedling")) == 20
    factory = KnowledgeFactory(region=_REGION, workspace_id=workspace_id, vault_root=vault_root)
    statements = await factory.retriever().retrieve_for_signals("payment settings")
    assert 0 < len(statements) <= 10
