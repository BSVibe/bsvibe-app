"""End-to-end integration test for ``IngestCompiler.compile_batch``.

Proves the full ingest path with a *real* LLM seam (the :class:`CompileLlm`
Protocol, not raw litellm) backed by a deterministic scripted extractor —
NEVER a real LLM or network call:

    labelled chunks
        -> compile_batch (extraction via the CompileLlm seam)
        -> garden notes + entity stubs written to the workspace vault
        -> graph nodes/edges materialised by GraphExtractor over the vault
        -> the written knowledge retrievable via VaultRetriever

Workspace isolation is structural: the vault is rooted at
``<vault_root>/<region>/<workspace_id>/`` via :class:`KnowledgeFactory`,
exactly the convention :class:`backend.knowledge.infrastructure.workers.settle_worker.KnowledgeSettleSink`
uses. A second workspace's compile lands in its own vault and never bleeds
into the first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.graph.graph_extractor import GraphExtractor
from backend.knowledge.ingest.file_index_reader import FileIndexReader
from backend.knowledge.ingest.ingest_compiler import (
    BatchItem,
    CompileLlm,
    IngestBatchRecord,
    IngestCompiler,
)
from backend.knowledge.retrieval.retriever import VaultRetriever

REGION = "us-1"
WORKSPACE_A = "11111111-1111-1111-1111-111111111111"
WORKSPACE_B = "22222222-2222-2222-2222-222222222222"


class ScriptedCompileLlm:
    """Deterministic :class:`CompileLlm` — returns canned plans, never a network.

    A list of raw plan strings is consumed one per ``chat`` call (one call
    per chunk). When the script is exhausted the last entry is reused so a
    chunk-count mismatch doesn't crash the test for the wrong reason.
    Records every call so the test can assert the seam contract
    (``suppress_reasoning`` forwarded, system prompt passed).
    """

    def __init__(self, plans: list[str]) -> None:
        self._plans = plans
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "suppress_reasoning": suppress_reasoning,
                "timeout_s": timeout_s,
            }
        )
        idx = min(len(self.calls) - 1, len(self._plans) - 1)
        return self._plans[idx]


def _plan(actions: list[dict[str, Any]]) -> str:
    return json.dumps(actions)


def _build_compiler(
    *,
    vault_root: Path,
    workspace_id: str,
    llm: CompileLlm,
) -> tuple[IngestCompiler, VaultRetriever, KnowledgeFactory]:
    """Construct a workspace-scoped compiler + retriever sharing one vault.

    Mirrors KnowledgeSettleSink: a per-workspace KnowledgeFactory roots the
    vault at ``<vault_root>/<region>/<workspace_id>/``; the writer and the
    retriever both hang off that same Vault, so a compile and the retrieval
    that proves it see identical paths.
    """
    factory = KnowledgeFactory(
        region=REGION,
        workspace_id=workspace_id,
        vault_root=vault_root,
    )
    writer = factory.writer()
    vault = factory.vault()
    vault.ensure_dirs()
    index_reader = FileIndexReader(vault)
    retriever = VaultRetriever(vault, index_reader=index_reader)
    compiler = IngestCompiler(
        garden_writer=writer,
        llm_client=llm,
        retriever=retriever,
        event_bus=None,
        max_updates=10,
    )
    return compiler, retriever, factory


def _materialise_graph(
    factory: KnowledgeFactory,
) -> tuple[list[Any], list[Any]]:
    """Run the deterministic GraphExtractor over every written garden note.

    Returns (entities, relationships) — the graph nodes/edges that emerge
    from the markdown + wikilinks compile_batch wrote to the vault.
    """
    extractor = GraphExtractor()
    root = factory.vault_path
    entities: list[Any] = []
    relationships: list[Any] = []
    for md_file in sorted(root.rglob("*.md")):
        rel_path = str(md_file.relative_to(root))
        content = md_file.read_text(encoding="utf-8")
        ents, rels = extractor.extract_from_note(rel_path, content)
        entities.extend(ents)
        relationships.extend(rels)
    return entities, relationships


@pytest.mark.asyncio
async def test_compile_batch_writes_graph_and_is_retrievable(tmp_path: Path) -> None:
    """Labelled chunks -> compile_batch -> vault graph -> retrievable."""
    plan = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Vaultwarden behind Caddy",
                "content": (
                    "Ran [[Vaultwarden]] behind [[Caddy]]. The "
                    "[[X-Forwarded-Proto]] header was the fix."
                ),
                "tags": ["self-hosting", "reverse-proxy"],
                "entities": ["[[Vaultwarden]]", "[[Caddy]]", "[[X-Forwarded-Proto]]"],
                "reason": "consolidates seeds #1 and #2 about Vaultwarden setup",
                "source_seeds": [1, 2],
                "related": [],
            },
            {
                "action": "create",
                "target_path": None,
                "title": "BSage graph backend",
                "content": "Notes on the [[BSage]] graph backend and [[NetworkX]].",
                "tags": ["knowledge-graph"],
                "entities": ["[[BSage]]", "[[NetworkX]]"],
                "reason": "captures seed #3 about BSage internals",
                "source_seeds": [3],
                "related": [],
            },
        ]
    )
    llm = ScriptedCompileLlm([plan])
    compiler, retriever, factory = _build_compiler(
        vault_root=tmp_path,
        workspace_id=WORKSPACE_A,
        llm=llm,
    )

    items = [
        BatchItem(
            label="vaultwarden.md",
            content="Tested Vaultwarden behind Caddy reverse proxy.",
        ),
        BatchItem(
            label="vaultwarden2.md",
            content="X-Forwarded-Proto was the issue for OAuth callbacks.",
        ),
        BatchItem(
            label="bsage.md",
            content="BSage uses NetworkX for the graph backend.",
        ),
    ]

    result = await compiler.compile_batch(items=items, seed_source="ai-memory-input")

    # 1. compile_batch ran the seam exactly once for one chunk and created
    #    the two planned notes.
    assert len(llm.calls) == 1
    assert result.llm_calls == 1
    assert result.notes_created == 2
    assert result.notes_updated == 0
    # The seam contract: structured-output suppression is forwarded and the
    # compile system prompt reaches the model.
    assert llm.calls[0]["suppress_reasoning"] is True
    # Lift E20 rewrote the prompt; the legacy "ingest compiler" header was
    # replaced by "knowledge garden curator". Either substring proves the
    # compile-batch prompt — not a tool-call / verifier prompt — was sent.
    assert "knowledge garden curator" in llm.calls[0]["system"].lower()

    # 2. Notes + entity stubs were written under the workspace-scoped vault.
    ws_root = factory.vault_path
    assert ws_root == tmp_path / REGION / WORKSPACE_A
    seedling = sorted((ws_root / "garden" / "seedling").glob("*.md"))
    assert len(seedling) == 2
    entity_stubs = {p.stem for p in (ws_root / "garden" / "entities").glob("*.md")}
    # Every wikilink target got a real vault file (graph extractor needs both
    # ends of each edge to exist).
    assert {"vaultwarden", "caddy", "bsage", "networkx"} <= entity_stubs

    # 3. Graph nodes + edges materialise from the written vault.
    entities, relationships = _materialise_graph(factory)
    entity_names = {e.name for e in entities}
    # Note entities (the garden notes themselves, named from their slug since
    # the body has no title frontmatter) are nodes...
    assert "vaultwarden behind caddy" in entity_names
    assert "bsage graph backend" in entity_names
    # ...and the wikilinked concepts are nodes too.
    assert "Vaultwarden" in entity_names
    assert "Caddy" in entity_names
    assert "BSage" in entity_names
    # Edges exist (note -> tag, note -> wikilinked concept, etc.).
    assert len(relationships) > 0
    rel_types = {r.rel_type for r in relationships}
    assert "tagged_with" in rel_types
    # The wikilinked concepts produce note->concept edges (the graph "emerges
    # from connections" — the whole point of the ingest compiler).
    target_ids = {e.id for e in entities if e.name in {"Vaultwarden", "Caddy", "BSage"}}
    assert any(r.target_id in target_ids for r in relationships)

    # 4. The written knowledge is retrievable through the real retriever.
    found = await retriever.search(query="Vaultwarden reverse proxy")
    assert "Vaultwarden behind Caddy" in found
    found_bsage = await retriever.search(query="BSage graph")
    assert "BSage graph backend" in found_bsage


@pytest.mark.asyncio
async def test_compile_batch_is_workspace_isolated(tmp_path: Path) -> None:
    """Workspace A's compile must never land in workspace B's vault."""
    plan_a = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Workspace A secret",
                "content": "Only [[WorkspaceA]] knows this.",
                "tags": ["alpha"],
                "entities": ["[[WorkspaceA]]"],
                "reason": "seed #1",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )
    plan_b = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Workspace B secret",
                "content": "Only [[WorkspaceB]] knows this.",
                "tags": ["beta"],
                "entities": ["[[WorkspaceB]]"],
                "reason": "seed #1",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )

    compiler_a, retriever_a, factory_a = _build_compiler(
        vault_root=tmp_path,
        workspace_id=WORKSPACE_A,
        llm=ScriptedCompileLlm([plan_a]),
    )
    compiler_b, retriever_b, factory_b = _build_compiler(
        vault_root=tmp_path,
        workspace_id=WORKSPACE_B,
        llm=ScriptedCompileLlm([plan_b]),
    )

    await compiler_a.compile_batch(
        items=[BatchItem(label="a.md", content="workspace a content")],
        seed_source="chat",
    )
    await compiler_b.compile_batch(
        items=[BatchItem(label="b.md", content="workspace b content")],
        seed_source="chat",
    )

    # Each vault root is distinct and holds only its own note.
    assert factory_a.vault_path != factory_b.vault_path
    a_notes = {p.stem for p in (factory_a.vault_path / "garden" / "seedling").glob("*.md")}
    b_notes = {p.stem for p in (factory_b.vault_path / "garden" / "seedling").glob("*.md")}
    assert "workspace-a-secret" in a_notes
    assert "workspace-b-secret" not in a_notes
    assert "workspace-b-secret" in b_notes
    assert "workspace-a-secret" not in b_notes

    # Retrieval is scoped — A's retriever cannot see B's note.
    found_a = await retriever_a.search(query="secret")
    assert "Workspace A secret" in found_a
    assert "Workspace B secret" not in found_a


@pytest.mark.asyncio
async def test_compile_batch_updates_existing_note_then_retrievable(tmp_path: Path) -> None:
    """A second compile that UPDATES an existing note stays retrievable.

    Guards the update path end to end (not just create): the plan targets
    a previously-written note, the body is rewritten, and the updated text
    surfaces through retrieval.
    """
    create_plan = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "AI Research",
                "content": "Early notes on [[AI]] research.",
                "tags": ["ai"],
                "entities": ["[[AI]]"],
                "reason": "seed #1",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )
    compiler, retriever, factory = _build_compiler(
        vault_root=tmp_path,
        workspace_id=WORKSPACE_A,
        llm=ScriptedCompileLlm([create_plan]),
    )
    first = await compiler.compile_batch(
        items=[BatchItem(label="ai.md", content="AI research notes")],
        seed_source="chat",
    )
    assert first.notes_created == 1

    written = next((factory.vault_path / "garden" / "seedling").glob("*.md"))
    rel_path = str(written.relative_to(factory.vault_path))

    update_plan = _plan(
        [
            {
                "action": "update",
                "target_path": rel_path,
                "title": "AI Research",
                "content": "# AI Research\n\nUpdated: now covering [[LLMs]] too.",
                "tags": ["ai"],
                "entities": ["[[LLMs]]"],
                "reason": "seed #2 adds LLM coverage",
                "source_seeds": [2],
                "related": [],
            }
        ]
    )
    update_compiler = IngestCompiler(
        garden_writer=factory.writer(),
        llm_client=ScriptedCompileLlm([update_plan]),
        retriever=retriever,
        event_bus=None,
        max_updates=10,
    )
    second = await update_compiler.compile_batch(
        items=[BatchItem(label="ai2.md", content="LLMs are part of AI research")],
        seed_source="chat",
    )
    assert second.notes_updated == 1
    assert second.notes_created == 0

    updated_text = written.read_text(encoding="utf-8")
    assert "LLMs" in updated_text

    # A fresh retriever (no stale in-memory index) sees the updated body.
    fresh_index = FileIndexReader(factory.vault())
    fresh_retriever = VaultRetriever(factory.vault(), index_reader=fresh_index)
    found = await fresh_retriever.search(query="AI research")
    assert "AI Research" in found


class _FakeBatchRecorder:
    """Captures :class:`IngestBatchRecord`s — stands in for the DB writer."""

    def __init__(self) -> None:
        self.records: list[IngestBatchRecord] = []

    async def record(self, record: IngestBatchRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_compile_batch_records_ingest_batch_row(tmp_path: Path) -> None:
    """compile_batch emits one analytics record per batch via the seam."""
    plan = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Note one",
                "content": "Body about [[Topic]].",
                "tags": ["topic"],
                "entities": ["[[Topic]]"],
                "reason": "seed #1",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )
    recorder = _FakeBatchRecorder()
    factory = KnowledgeFactory(region=REGION, workspace_id=WORKSPACE_A, vault_root=tmp_path)
    factory.vault().ensure_dirs()
    compiler = IngestCompiler(
        garden_writer=factory.writer(),
        llm_client=ScriptedCompileLlm([plan]),
        retriever=None,
        event_bus=None,
        max_updates=10,
        batch_recorder=recorder,
    )

    result = await compiler.compile_batch(
        items=[
            BatchItem(label="a.md", content="seed a"),
            BatchItem(label="b.md", content="seed b"),
        ],
        seed_source="ai-memory-input",
    )

    assert len(recorder.records) == 1
    row = recorder.records[0]
    assert row.seed_source == "ai-memory-input"
    assert row.seed_count == 2
    assert row.notes_created == 1
    assert row.notes_updated == 0
    assert row.llm_calls == 1
    assert row.chunk_count == 1
    assert row.chunk_failures == 0
    assert row.elapsed_ms >= 0
    # The result mirrors the recorded telemetry.
    assert result.seed_count == 2
    assert result.elapsed_ms == row.elapsed_ms


@pytest.mark.asyncio
async def test_empty_batch_does_not_record(tmp_path: Path) -> None:
    """An empty batch short-circuits before any analytics row is recorded."""
    recorder = _FakeBatchRecorder()
    factory = KnowledgeFactory(region=REGION, workspace_id=WORKSPACE_A, vault_root=tmp_path)
    factory.vault().ensure_dirs()
    compiler = IngestCompiler(
        garden_writer=factory.writer(),
        llm_client=ScriptedCompileLlm(["[]"]),
        retriever=None,
        event_bus=None,
        batch_recorder=recorder,
    )

    result = await compiler.compile_batch(items=[], seed_source="chat")

    assert recorder.records == []
    assert result.notes_created == 0


@pytest.mark.asyncio
async def test_recorder_failure_does_not_break_ingest(tmp_path: Path) -> None:
    """A recorder that raises must not sink an otherwise-successful compile."""

    class _BoomRecorder:
        async def record(self, record: IngestBatchRecord) -> None:
            raise RuntimeError("db down")

    plan = _plan(
        [
            {
                "action": "create",
                "target_path": None,
                "title": "Survivor",
                "content": "Content about [[Thing]].",
                "tags": ["x"],
                "entities": ["[[Thing]]"],
                "reason": "seed #1",
                "source_seeds": [1],
                "related": [],
            }
        ]
    )
    factory = KnowledgeFactory(region=REGION, workspace_id=WORKSPACE_A, vault_root=tmp_path)
    factory.vault().ensure_dirs()
    compiler = IngestCompiler(
        garden_writer=factory.writer(),
        llm_client=ScriptedCompileLlm([plan]),
        retriever=None,
        event_bus=None,
        batch_recorder=_BoomRecorder(),
    )

    result = await compiler.compile_batch(
        items=[BatchItem(label="a.md", content="seed")],
        seed_source="chat",
    )
    # The note was still written despite the recorder blowing up.
    assert result.notes_created == 1
    assert (factory.vault_path / "garden" / "seedling" / "survivor.md").exists()
