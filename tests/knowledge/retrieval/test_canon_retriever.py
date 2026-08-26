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

from backend.knowledge import KnowledgeFactory
from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage
from backend.workflow.application.verification_service import CanonRetriever

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
    initial_body: str | None = None,
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
        ),
        initial_body=initial_body,
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


async def test_concept_body_substance_folds_into_statement(vault_root: Path) -> None:
    """KG Lift 4 — a matched concept surfaces its SYNTHESIZED body substance (the
    member excerpts), not just the bare title, so Lift 1's content reaches the
    verify/answer context."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="idempotency",
        display="Idempotency",
        initial_body=(
            "Synthesized from 1 garden observation:\n\n"
            "- [[create-ref-422-reuse]] — create_ref returning 422 means the branch "
            "already exists; re-fetch and reuse it instead of failing."
        ),
    )
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)
    retriever = factory.retriever()

    patterns = await retriever.retrieve_for_signals("improved idempotency of create_ref\napi.py")

    # The substantive member excerpt reaches the contract (not just "Idempotency").
    assert any("re-fetch and reuse it instead of failing" in s for s in patterns), patterns
    # And it's still anchored to the concept title.
    assert any(s.startswith("Idempotency") for s in patterns), patterns


async def test_bodyless_concept_still_returns_title(vault_root: Path) -> None:
    """Back-compat: a concept with no synthesized body surfaces its title alone."""
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

    patterns = await retriever.retrieve_for_signals("dependency pinning\nrequirements.txt")
    assert "Always pin dependency versions" in patterns


# --------------------------------------------------------------------------
# A RETRACTED concept must never ground anything
# --------------------------------------------------------------------------
#
# ``answer_grounding._expand`` already states the rule for notes: "Retraction has
# to be honoured at every consumer, not just at the writer… the founder then gets
# their own retracted knowledge quoted back as fact (prod, 2026-07-13)". It then
# exempts every other kind (``if item.kind != "note": return item``), and THIS
# retriever — the only producer of ``kind="concept"`` items — never looks at
# ``retracted_at`` at all.
#
# Measured in prod 2026-08-26 (qazasa123 vault): 403 of 932 files under
# `concepts/active/` carry a `retracted_at` stamp, one of them reading
# `retraction_reason: cleanup - full reset`. Retraction stamps the frontmatter
# and leaves the file where it is, so "active" means "in the active folder",
# not "not retracted". `concept_graph.py:87` already filters this way; the
# retrieval path does not.


async def _retract(vault_root: Path, *, region: str, workspace_id: str, concept_id: str) -> None:
    """Stamp a concept the way retraction does — frontmatter only, file stays put."""
    path = vault_root / region / workspace_id / "concepts" / "active" / f"{concept_id}.md"
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), "seeded concept must have frontmatter"
    head, sep, rest = raw[4:].partition("---\n")
    stamped = (
        "---\n"
        + head
        + "retracted_at: '2026-07-10T07:19:25.705384+00:00'\n"
        + "retraction_reason: cleanup - full reset\n"
        + sep
        + rest
    )
    path.write_text(stamped, encoding="utf-8")


async def test_a_retracted_concept_never_reaches_the_contract(vault_root: Path) -> None:
    """The founder retracted it. It must not come back as a canonical pattern."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    await _retract(vault_root, region=_REGION, workspace_id=ws, concept_id="dependency-pinning")
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)

    patterns = await factory.retriever().retrieve_for_signals(
        "Updated dependency pinning in the lockfile\nrequirements.txt"
    )
    assert patterns == [], f"retracted concept leaked into the contract: {patterns}"


async def test_a_retracted_concept_is_absent_from_the_structured_surface(
    vault_root: Path,
) -> None:
    """``retrieve_structured`` is what deep-links the report chip. A retracted
    concept must not be linkable there either — same knowledge, second door."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    await _retract(vault_root, region=_REGION, workspace_id=ws, concept_id="dependency-pinning")
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)

    items = await factory.retriever().retrieve_structured(
        "Updated dependency pinning in the lockfile"
    )
    assert [i.ref for i in items] == []


async def test_a_live_concept_still_surfaces_next_to_a_retracted_one(
    vault_root: Path,
) -> None:
    """POSITIVE CONTROL — the fix must remove the retracted one and NOTHING else.
    A filter that empties the surface would 'pass' the two tests above while
    silently deleting the retrieval feature."""
    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="stateless-design",
        display="Keep handlers stateless",
    )
    await _retract(vault_root, region=_REGION, workspace_id=ws, concept_id="dependency-pinning")
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)

    patterns = await factory.retriever().retrieve_for_signals(
        "dependency pinning and stateless design in the handler"
    )
    assert "Keep handlers stateless" in patterns
    assert "Always pin dependency versions" not in patterns


async def test_a_workspace_whose_concepts_are_all_retracted_takes_the_cheap_exit(
    vault_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap-exit asks "does this workspace have anything to give?". If every
    concept is retracted the honest answer is NO — and the difference from
    "yes, then filter each one away" is not the result (both end up empty) but
    the COST: the latter resolves every candidate token against the registry on
    every signal, forever, for a workspace that can never contribute.

    So this asserts the resolver is never reached. Asserting ``== []`` alone
    passes either way — the per-concept filter already produces it — which is
    exactly what the negative control showed."""
    from backend.knowledge.canonicalization import resolver as _resolver

    ws = _ws()
    await _seed_concept(
        vault_root,
        region=_REGION,
        workspace_id=ws,
        concept_id="dependency-pinning",
        display="Always pin dependency versions",
    )
    await _retract(vault_root, region=_REGION, workspace_id=ws, concept_id="dependency-pinning")
    factory = KnowledgeFactory(region=_REGION, workspace_id=ws, vault_root=vault_root)

    resolves: list[str] = []
    original = _resolver.TagResolver.resolve

    async def _counting(self: object, tag: str) -> object:
        resolves.append(tag)
        return await original(self, tag)  # type: ignore[arg-type]

    monkeypatch.setattr(_resolver.TagResolver, "resolve", _counting)

    assert await factory.retriever().retrieve_for_signals("dependency pinning") == []
    assert resolves == [], f"resolved {len(resolves)} tags for a workspace with nothing live"
