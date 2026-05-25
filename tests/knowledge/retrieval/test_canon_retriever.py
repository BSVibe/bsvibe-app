"""CanonRetriever — high-precision canonical-pattern retrieval (B3).

The retriever is the seam the verifier folds into a verify contract
(``retrieve_for_signals(signals) -> list[str]``). It must surface the
workspace's PROMOTED canonical concepts relevant to a change's signals — never
arbitrary garden notes — and must degrade gracefully (empty / unknown
workspace → ``[]``) so an empty-knowledge workspace sees no verify behaviour
change. It must NEVER raise into the verify path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from backend.execution.verifier.service import CanonRetriever
from backend.knowledge import KnowledgeFactory
from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


def _ws() -> str:
    return str(uuid.uuid4())


async def _seed_concept(
    vault_root: Path,
    *,
    region: str,
    workspace_id: str,
    concept_id: str,
    display: str,
    aliases: list[str] | None = None,
) -> None:
    """Write a promoted active concept into the workspace's vault on disk."""
    store = NoteStore(FileSystemStorage(vault_root / region / workspace_id))
    await store.write_concept(
        models.ConceptEntry(
            concept_id=concept_id,
            path=f"concepts/active/{concept_id}.md",
            display=display,
            aliases=list(aliases or []),
            created_at=datetime(2026, 5, 6),
            updated_at=datetime(2026, 5, 6),
        )
    )


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


async def test_retriever_satisfies_canon_retriever_protocol(vault_root: Path) -> None:
    factory = KnowledgeFactory(region=_REGION, workspace_id=_ws(), vault_root=vault_root)
    retriever = factory.retriever()
    assert isinstance(retriever, CanonRetriever)


async def test_empty_workspace_returns_no_patterns(vault_root: Path) -> None:
    """No-canon workspace → []: an empty-knowledge workspace sees NO verify
    behaviour change (the central graceful-empty invariant)."""
    factory = KnowledgeFactory(region=_REGION, workspace_id=_ws(), vault_root=vault_root)
    retriever = factory.retriever()
    assert await retriever.retrieve_for_signals("anything at all\nsrc/x.py") == []


async def test_unknown_workspace_with_no_vault_returns_empty(vault_root: Path) -> None:
    """A workspace whose vault dir never materialized must not raise."""
    factory = KnowledgeFactory(region=_REGION, workspace_id=_ws(), vault_root=vault_root)
    retriever = factory.retriever()
    assert await retriever.retrieve_for_signals("some change") == []


async def test_matching_signal_surfaces_canonical_concept(vault_root: Path) -> None:
    """A canonical concept whose id/tokens appear in the signals is returned as
    a pattern statement."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    patterns = await retriever.retrieve_for_signals(
        "Updated dependency pinning in the lockfile\nrequirements.txt"
    )
    assert "Always pin dependency versions" in patterns


async def test_alias_match_surfaces_concept(vault_root: Path) -> None:
    """A signal token matching a concept ALIAS still resolves to the concept."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="structured-logging",
        display="Use structlog for structured logging",
        aliases=["structlog"],
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    patterns = await retriever.retrieve_for_signals("switched prints to structlog calls\napp.py")
    assert "Use structlog for structured logging" in patterns


async def test_non_matching_signal_returns_empty(vault_root: Path) -> None:
    """High precision: an unrelated change surfaces NO concept (no spurious
    folding into the contract)."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    patterns = await retriever.retrieve_for_signals("renamed a CSS class in the footer\nfooter.css")
    assert patterns == []


async def test_results_are_capped(vault_root: Path) -> None:
    """At most 5 patterns even when many concepts match — a bounded fold."""
    ws = _ws()
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    for word in words:
        await _seed_concept(
            vault_root,
            region=_REGION,
            workspace_id=ws,
            concept_id=word,
            display=f"{word.title()} statement",
        )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    signals = " ".join(words)
    patterns = await retriever.retrieve_for_signals(signals)
    assert 0 < len(patterns) <= 5


async def test_workspace_isolation(vault_root: Path) -> None:
    """A retriever bound to workspace A never surfaces workspace B's canon."""
    ws_a, ws_b = _ws(), _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws_b,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    retriever_a = KnowledgeFactory(
        region=_REGION, workspace_id=ws_a, vault_root=vault_root
    ).retriever()
    assert await retriever_a.retrieve_for_signals("dependency pinning change") == []


async def test_retrieve_never_raises_on_storage_error(vault_root: Path) -> None:
    """A read failure mid-retrieval degrades to [] — never raises into verify."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    # Corrupt the vault root mid-flight so initialize/list raises internally; the
    # public method must still return [] rather than propagating.
    import shutil

    shutil.rmtree(factory.vault_path)
    factory.vault_path.write_text("not a directory")  # make the path a FILE
    assert await retriever.retrieve_for_signals("dependency pinning") == []
