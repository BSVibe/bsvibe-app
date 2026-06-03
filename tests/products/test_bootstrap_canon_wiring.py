"""Lift A-fix — bootstrap registers canonical anchors so the graph view has nodes.

Diagnostic: Lift A's bootstrap built a Knowledge facade whose IngestCompiler
was constructed WITHOUT a ``canonicalization_service``, so per-tag canonicalize
silently no-op'd and ``concepts/active/<id>.md`` files were never created. The
PWA Knowledge graph reads ``InMemoryCanonicalizationIndex.list_active_concepts``
which scans ``concepts/active/`` — empty directory → empty graph.

These tests pin the fix: bootstrap must run promotion against the workspace
vault after ingest so every recurring tag the LLM committed becomes a
``concepts/active/<id>.md`` anchor (the SoT the graph view reads).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.paths import active_concept_path
from backend.knowledge.graph.storage import FileSystemStorage
from backend.products.application.bootstrap.anchor_backfill import (
    register_bootstrap_anchors,
)

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


def _seed_observation(storage_root: Path, slug: str, tags: list[str]) -> None:
    """Write a settle-style garden observation referencing ``tags``."""
    path = storage_root / "garden" / "seedling" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    path.write_text(
        f"---\ntags:\n{tag_yaml}\n---\n# {slug}\n\nbody for {slug}\n",
        encoding="utf-8",
    )


async def test_register_bootstrap_anchors_creates_active_concept_for_recurring_tag(
    tmp_path: Path,
) -> None:
    """A tag that recurs across ≥2 observations becomes a ``concepts/active/`` file."""
    workspace_id = uuid.uuid4()
    vault = tmp_path / _REGION / str(workspace_id)
    vault.mkdir(parents=True)

    # Two observations both mentioning ``self-hosting`` → recurrence gate passes.
    _seed_observation(vault, "obs-1", ["settle", "verified-run", "self-hosting"])
    _seed_observation(vault, "obs-2", ["settle", "verified-run", "self-hosting"])

    storage = FileSystemStorage(vault)
    result = await register_bootstrap_anchors(storage)

    # The promoter created the concept.
    assert "self-hosting" in result.created_concepts
    concept_file = vault / active_concept_path("self-hosting")
    assert concept_file.exists(), "concepts/active/self-hosting.md must exist"
    text = concept_file.read_text(encoding="utf-8")
    # Display is a Title-Cased rendering of the raw tag (split on '-' / '_').
    assert "# Self-hosting" in text or "# Self-Hosting" in text

    # The graph view's source-of-truth index sees the new concept.
    fresh_index = InMemoryCanonicalizationIndex()
    await fresh_index.initialize(storage)
    concepts = await fresh_index.list_active_concepts()
    assert any(c.concept_id == "self-hosting" for c in concepts)


async def test_register_bootstrap_anchors_skips_one_off_tags(tmp_path: Path) -> None:
    """Tags with a single mention stay in the noise pile (natural decay policy)."""
    workspace_id = uuid.uuid4()
    vault = tmp_path / _REGION / str(workspace_id)
    vault.mkdir(parents=True)

    # ``one-off`` appears in exactly one observation → below recurrence gate.
    _seed_observation(vault, "obs-1", ["settle", "verified-run", "one-off"])

    storage = FileSystemStorage(vault)
    result = await register_bootstrap_anchors(storage)

    assert "one-off" not in result.created_concepts
    concept_file = vault / "concepts" / "active" / "one-off.md"
    assert not concept_file.exists()


async def test_register_bootstrap_anchors_idempotent_on_rerun(tmp_path: Path) -> None:
    """Re-running on the same vault adds nothing (resolver dedups)."""
    workspace_id = uuid.uuid4()
    vault = tmp_path / _REGION / str(workspace_id)
    vault.mkdir(parents=True)

    _seed_observation(vault, "obs-1", ["settle", "verified-run", "graph-view"])
    _seed_observation(vault, "obs-2", ["settle", "verified-run", "graph-view"])

    storage = FileSystemStorage(vault)
    first = await register_bootstrap_anchors(storage)
    assert "graph-view" in first.created_concepts

    second = await register_bootstrap_anchors(storage)
    assert "graph-view" not in second.created_concepts  # already exists, resolved

    fresh_index = InMemoryCanonicalizationIndex()
    await fresh_index.initialize(storage)
    ids = [c.concept_id for c in await fresh_index.list_active_concepts()]
    # Exactly one concept survives — no duplicates.
    assert ids.count("graph-view") == 1


async def test_register_bootstrap_anchors_empty_vault_no_concepts(tmp_path: Path) -> None:
    """A vault with no garden notes yields no concepts (no crash)."""
    workspace_id = uuid.uuid4()
    vault = tmp_path / _REGION / str(workspace_id)
    vault.mkdir(parents=True)

    storage = FileSystemStorage(vault)
    result = await register_bootstrap_anchors(storage)

    assert result.created_concepts == []
    assert result.candidate_tags == []


async def test_concept_graph_has_nodes_after_anchor_registration(tmp_path: Path) -> None:
    """End-to-end: after register_bootstrap_anchors, build_concept_graph sees nodes.

    Pins the diagnostic: the PWA Knowledge graph endpoint sources its picture
    from ``build_concept_graph`` which reads ``list_active_concepts``. Before
    the fix that returned ``[]``; after the fix it returns one node per
    recurring entity.
    """
    from backend.knowledge.canonicalization.concept_graph import build_concept_graph

    workspace_id = uuid.uuid4()
    vault = tmp_path / _REGION / str(workspace_id)
    vault.mkdir(parents=True)

    # Three distinct entities, each recurring → three concept nodes expected.
    _seed_observation(vault, "obs-1", ["settle", "verified-run", "vault-backend"])
    _seed_observation(vault, "obs-2", ["settle", "verified-run", "vault-backend"])
    _seed_observation(vault, "obs-3", ["settle", "verified-run", "ingest-compiler"])
    _seed_observation(vault, "obs-4", ["settle", "verified-run", "ingest-compiler"])
    _seed_observation(vault, "obs-5", ["settle", "verified-run", "graph-view"])
    _seed_observation(vault, "obs-6", ["settle", "verified-run", "graph-view"])

    storage = FileSystemStorage(vault)
    await register_bootstrap_anchors(storage)

    graph = await build_concept_graph(storage)
    node_ids = {str(n) for n in graph.nodes()}
    assert {"vault-backend", "ingest-compiler", "graph-view"}.issubset(node_ids), (
        f"expected three concept nodes, got {node_ids}"
    )
