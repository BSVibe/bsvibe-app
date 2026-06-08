"""Tests for backend.knowledge.ingest.ingest_compiler — IngestCompiler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter


async def _compile(compiler: Any, content: str, source: str) -> Any:
    """Call compile_batch with a single item — mirrors the old compile() shape.

    Kept as a test helper so the existing single-seed scenarios stay
    readable. Production code goes through compile_batch directly.
    """
    from backend.knowledge.ingest.ingest_compiler import BatchItem

    return await compiler.compile_batch(
        items=[BatchItem(label=source, content=content)],
        seed_source=source,
    )


class TestIngestCompilerCompile:
    """Test IngestCompiler.compile_batch() core behaviour via single-item batches."""

    @pytest.fixture()
    def vault_and_writer(self, tmp_path: Path) -> tuple[Vault, GardenWriter]:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        return vault, GardenWriter(vault)

    @pytest.fixture()
    def mock_llm(self) -> AsyncMock:
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value="[]")
        return llm

    @pytest.fixture()
    def mock_retriever(self) -> AsyncMock:
        retriever = AsyncMock()
        retriever.search = AsyncMock(return_value="No notes found.")
        return retriever

    @pytest.fixture()
    def mock_event_bus(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def compiler(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> Any:
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        _, writer = vault_and_writer
        return IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )

    @pytest.mark.asyncio
    async def test_compile_returns_compile_result(self, compiler: Any) -> None:
        """compile() should return a CompileResult dataclass."""
        from backend.knowledge.ingest.ingest_compiler import CompileResult

        result = await _compile(compiler, "Some new information about AI", "telegram-input")
        assert isinstance(result, CompileResult)
        assert isinstance(result.notes_updated, int)
        assert isinstance(result.notes_created, int)
        assert isinstance(result.actions_taken, list)

    @pytest.mark.asyncio
    async def test_compile_calls_retriever_search(
        self, compiler: Any, mock_retriever: AsyncMock
    ) -> None:
        """compile() should search for related existing notes."""
        await _compile(compiler, "New insight about knowledge graphs", "chat")
        mock_retriever.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compile_calls_llm_for_plan(self, compiler: Any, mock_llm: AsyncMock) -> None:
        """compile() should ask LLM to plan updates."""
        await _compile(compiler, "New data about project BSage", "telegram-input")
        mock_llm.chat.assert_awaited_once()
        # System prompt should mention ingest compilation
        call_kwargs = mock_llm.chat.call_args
        assert "system" in call_kwargs.kwargs or len(call_kwargs.args) >= 1

    @pytest.mark.asyncio
    async def test_compile_creates_new_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When LLM plans 'create' actions, new garden notes should appear."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        vault, writer = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": "Knowledge Graphs Overview",
                    "content": "Knowledge graphs connect entities and relationships.",
                    "note_type": "insight",
                    "reason": "New concept from seed",
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )
        result = await _compile(compiler, "Knowledge graphs are powerful", "telegram-input")

        assert result.notes_created == 1
        assert result.notes_updated == 0
        # Post dynamic-ontology refactor: ingest_compiler stops asking the LLM
        # to classify notes, so we no longer fan out to per-type folders.
        # New notes land in the temporary "ideas/" holding area until
        # Step B3 swaps in the maturity-based layout.
        seedling_dir = vault.root / "garden" / "seedling"
        md_files = list(seedling_dir.glob("*.md"))
        assert len(md_files) >= 1
        content = md_files[0].read_text()
        assert "Knowledge Graphs Overview" in content

    @pytest.mark.asyncio
    async def test_compile_updates_existing_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When LLM plans 'update' actions, existing notes should be modified."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        vault, writer = vault_and_writer

        # Create an existing note first
        from backend.knowledge.graph.writer import GardenNote

        await writer.write_garden(
            GardenNote(
                title="AI Research",
                content="Early research on AI.",
                source="manual",
            )
        )
        existing_path = "garden/seedling/ai-research.md"

        plan = json.dumps(
            [
                {
                    "action": "update",
                    "target_path": existing_path,
                    "title": "AI Research",
                    "content": "# AI Research\n\nUpdated: AI research now includes LLMs.",
                    "note_type": "insight",
                    "reason": "New information from seed",
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )
        result = await _compile(compiler, "LLMs are transforming AI", "chat")

        assert result.notes_updated == 1
        assert result.notes_created == 0
        updated = (vault.root / existing_path).read_text()
        assert "LLMs" in updated

    @pytest.mark.asyncio
    async def test_compile_appends_to_existing_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When LLM plans 'append' actions, text should be appended."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        vault, writer = vault_and_writer
        from backend.knowledge.graph.writer import GardenNote

        await writer.write_garden(
            GardenNote(
                title="Machine Learning",
                content="ML is a subset of AI.",
                source="manual",
            )
        )
        existing_path = "garden/seedling/machine-learning.md"

        plan = json.dumps(
            [
                {
                    "action": "append",
                    "target_path": existing_path,
                    "title": "Machine Learning",
                    "content": "\n## New Section\n\nDeep learning advances.",
                    "note_type": "idea",
                    "reason": "Additional information",
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )
        result = await _compile(compiler, "Deep learning is advancing fast", "chat")

        assert result.notes_updated == 1
        content = (vault.root / existing_path).read_text()
        assert "Deep learning advances" in content

    @pytest.mark.asyncio
    async def test_compile_respects_max_updates(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """compile() should cap the number of actions to max_updates."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        _, writer = vault_and_writer
        # LLM returns 5 create actions but max_updates is 2
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": f"Note {i}",
                    "content": f"Content {i}",
                    "note_type": "idea",
                    "reason": "test",
                    "related": [],
                }
                for i in range(5)
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=2,
        )
        result = await _compile(compiler, "Many topics", "test")

        assert result.notes_created <= 2
        assert len(result.actions_taken) <= 2

    @pytest.mark.asyncio
    async def test_compile_emits_events(
        self,
        compiler: Any,
        mock_llm: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """compile() should emit INGEST_COMPILE_START and INGEST_COMPILE_COMPLETE events."""
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": "Test",
                    "content": "Test content",
                    "note_type": "idea",
                    "reason": "test",
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        await _compile(compiler, "test data", "test-source")

        # Check that emit was called (via emit_event helper)
        assert mock_event_bus.emit.await_count >= 2

    @pytest.mark.asyncio
    async def test_compile_handles_empty_plan(self, compiler: Any, mock_llm: AsyncMock) -> None:
        """When LLM returns empty plan, compile() should return zero counts."""
        mock_llm.chat = AsyncMock(return_value="[]")

        result = await _compile(compiler, "irrelevant data", "test")

        assert result.notes_created == 0
        assert result.notes_updated == 0
        assert result.actions_taken == []

    @pytest.mark.asyncio
    async def test_compile_handles_malformed_llm_response(
        self, compiler: Any, mock_llm: AsyncMock
    ) -> None:
        """When LLM returns invalid JSON, compile() should not crash."""
        mock_llm.chat = AsyncMock(return_value="This is not JSON at all")

        result = await _compile(compiler, "some data", "test")

        assert result.notes_created == 0
        assert result.notes_updated == 0

    @pytest.mark.asyncio
    async def test_compile_handles_llm_errors_without_breaking_ingestion(
        self, compiler: Any, mock_llm: AsyncMock
    ) -> None:
        """Ingest compilation is best-effort; LLM/auth outages must not turn
        input ingestion into an HTTP 500."""
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("Missing API key"))

        result = await _compile(compiler, "some data", "bsnexus-input")

        assert result.notes_created == 0
        assert result.notes_updated == 0
        assert result.actions_taken == []

    @pytest.mark.asyncio
    async def test_compile_updates_cross_references(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When LLM specifies related links, they should be added to the note."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        _, writer = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": "Neural Networks",
                    "content": "Neural networks are the basis of deep learning.",
                    "note_type": "insight",
                    "reason": "New concept",
                    "related": ["Machine Learning", "AI Research"],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )
        result = await _compile(compiler, "Neural nets are powerful", "test")

        assert result.notes_created == 1
        vault = vault_and_writer[0]
        note_files = list((vault.root / "garden" / "seedling").glob("*.md"))
        content = note_files[0].read_text()
        assert "[[Machine Learning]]" in content or "Machine Learning" in content

    @pytest.mark.asyncio
    async def test_compile_skips_invalid_actions(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
        mock_retriever: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Actions with missing required fields should be skipped."""
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        _, writer = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "title": "Valid",
                    "content": "ok",
                    "note_type": "idea",
                    "reason": "test",
                    "related": [],
                },
                {"action": "create"},  # missing fields
                {
                    "action": "update",
                    "target_path": "nonexistent/path.md",
                    "title": "X",
                    "content": "x",
                    "note_type": "idea",
                    "reason": "test",
                    "related": [],
                },
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=mock_retriever,
            event_bus=mock_event_bus,
            max_updates=10,
        )
        result = await _compile(compiler, "test", "test")

        # Only the valid 'create' should succeed; malformed and nonexistent update should be skipped
        assert result.notes_created == 1
        assert len(result.actions_taken) == 1


class TestIngestCompilerCompileBatch:
    """compile_batch consolidates N seeds into a single LLM plan."""

    @pytest.fixture()
    def vault_and_writer(self, tmp_path: Path) -> tuple[Vault, GardenWriter]:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        return vault, GardenWriter(vault)

    @pytest.fixture()
    def mock_llm(self) -> AsyncMock:
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value="[]")
        return llm

    def _make_compiler(
        self,
        writer: GardenWriter,
        mock_llm: AsyncMock,
        *,
        max_updates: int = 10,
        batch_char_budget: int | None = None,
    ):
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        return IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=None,
            event_bus=None,
            max_updates=max_updates,
            batch_char_budget=batch_char_budget,
        )

    @pytest.mark.asyncio
    async def test_single_llm_call_for_many_items(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        compiler = self._make_compiler(writer, mock_llm)

        items = [BatchItem(label=f"f{i}.md", content=f"# Note {i}\nbody") for i in range(5)]
        result = await compiler.compile_batch(items=items, seed_source="test")

        # 5 items → exactly 1 LLM chat call (the whole point of batching).
        assert mock_llm.chat.await_count == 1
        assert result.llm_calls == 1

    @pytest.mark.asyncio
    async def test_batch_creates_consolidated_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        # LLM returns one consolidated note covering both seeds.
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "uv pinning policy",
                    "content": "Always pin Python via uv. Source: seeds #1 and #2.",
                    "note_type": "preference",
                    "reason": "consolidates seeds #1, #2 (both describe uv pinning)",
                    "related": [],
                    "source_seeds": [1, 2],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = self._make_compiler(writer, mock_llm)

        items = [
            BatchItem(label="claude-code/feedback_uv.md", content="Pin via uv"),
            BatchItem(label="claude-code/feedback_uv2.md", content="Always uv pin"),
        ]
        result = await compiler.compile_batch(items=items, seed_source="claude-code")

        assert result.notes_created == 1
        # Two seeds collapsed into one garden note — that's the win.
        assert len(result.actions_taken) == 1

    @pytest.mark.asyncio
    async def test_oversized_batch_chunks_into_multiple_calls(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        # When combined seeds exceed the configured char budget, the
        # compiler chunks them so no single LLM call gets a stuffed prompt.
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        budget = 4_000
        compiler = self._make_compiler(writer, mock_llm, batch_char_budget=budget)

        big = "x" * (budget // 2 + 1)
        items = [
            BatchItem(label="a.md", content=big),
            BatchItem(label="b.md", content=big),
            BatchItem(label="c.md", content=big),
        ]
        await compiler.compile_batch(items=items, seed_source="test")
        # 3 oversized items → 3 chunks → 3 LLM calls.
        assert mock_llm.chat.await_count == 3

    @pytest.mark.asyncio
    async def test_per_chunk_related_lookup(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        # Each chunk asks the retriever fresh with its own seeds — not
        # the previous "compute once with items[:3]" shape.
        from backend.knowledge.ingest.ingest_compiler import BatchItem, IngestCompiler

        _, writer = vault_and_writer
        retriever = AsyncMock()
        retriever.search = AsyncMock(return_value="No notes.")

        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=retriever,
            event_bus=None,
            max_updates=10,
            batch_char_budget=4_000,
        )

        big = "y" * 3_000
        items = [
            BatchItem(label="a.md", content=big),
            BatchItem(label="b.md", content=big),
            BatchItem(label="c.md", content=big),
        ]
        await compiler.compile_batch(items=items, seed_source="test")
        # 3 chunks → 3 retriever lookups (was 1 lookup shared across all).
        assert retriever.search.await_count == 3

    @pytest.mark.asyncio
    async def test_empty_batch_skips_llm(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        _, writer = vault_and_writer
        compiler = self._make_compiler(writer, mock_llm)

        result = await compiler.compile_batch(items=[], seed_source="test")
        assert mock_llm.chat.await_count == 0
        assert result.notes_created == 0
        assert result.notes_updated == 0

    @pytest.mark.asyncio
    async def test_partial_chunk_failure_preserves_earlier_results(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_llm: AsyncMock,
    ) -> None:
        """Surfaced by the real-vault import smoke test — when the first
        chunk succeeds and a later one raises (LLM timeout, malformed
        JSON, ollama process crash), the whole compile previously
        threw away the in-progress count even though the writes were
        already on disk. Now per-chunk failures only burn that chunk."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        good_plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "First chunk note",
                    "content": "Content",
                    "tags": ["test"],
                    "entities": [],
                    "reason": "test",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        # First call succeeds, second raises mid-compile.
        mock_llm.chat = AsyncMock(side_effect=[good_plan, RuntimeError("LLM down")])
        # Force two chunks by giving each item more than half the budget.
        compiler = self._make_compiler(writer, mock_llm, batch_char_budget=4_000)

        big = "x" * 3_000
        items = [
            BatchItem(label="a.md", content=big),
            BatchItem(label="b.md", content=big),
        ]
        result = await compiler.compile_batch(items=items, seed_source="partial")

        assert mock_llm.chat.await_count == 2
        # The successful chunk's count survives the second chunk's failure.
        assert result.notes_created == 1
        assert len(result.actions_taken) == 1
        assert result.llm_calls == 1


class TestDynamicOntologyContract:
    """Step B1 — compile_batch produces tag- and entity-rich plans, not a
    note_type pick. The LLM-output cleaners enforce the ``tags``/``entities``
    contract documented in COMPILE_BATCH_SYSTEM_PROMPT (kind tag blocklist,
    wikilink validation, count caps)."""

    @pytest.fixture()
    def vault_and_writer(self, tmp_path: Path) -> tuple[Vault, GardenWriter]:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        return vault, GardenWriter(vault)

    @pytest.fixture()
    def mock_llm(self) -> AsyncMock:
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value="[]")
        return llm

    def _make_compiler(self, writer: GardenWriter, mock_llm: AsyncMock):
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        return IngestCompiler(
            garden_writer=writer,
            llm_client=mock_llm,
            retriever=None,
            event_bus=None,
            max_updates=10,
        )

    @pytest.mark.asyncio
    async def test_compile_batch_forwards_suppress_reasoning_to_llm(
        self, vault_and_writer: tuple[Vault, GardenWriter], mock_llm: AsyncMock
    ) -> None:
        """compile_batch must call llm.chat(suppress_reasoning=True).

        Compile-time output is a structured JSON array. CoT prefixes
        from reasoning models would corrupt the parse, so the compiler
        is the canonical caller of suppression."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        compiler = self._make_compiler(writer, mock_llm)
        await compiler.compile_batch(
            items=[BatchItem(label="x.md", content="hello world")],
            seed_source="test",
        )
        kwargs = mock_llm.chat.await_args.kwargs
        assert kwargs.get("suppress_reasoning") is True

    @pytest.mark.asyncio
    async def test_kind_tags_are_filtered_out(
        self, vault_and_writer: tuple[Vault, GardenWriter], mock_llm: AsyncMock
    ) -> None:
        """LLM may slip through forbidden 'kind' tags — strip them so the
        graph doesn't repopulate the type filing cabinet via tags."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        vault, writer = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "Test note",
                    "content": "Just some content",
                    "tags": ["idea", "self-hosting", "fact", "reverse-proxy"],
                    "entities": [],
                    "reason": "test",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = self._make_compiler(writer, mock_llm)
        result = await compiler.compile_batch(
            items=[BatchItem(label="x.md", content="hello")], seed_source="test"
        )

        assert result.notes_created == 1
        action = result.actions_taken[0]
        # "idea" and "fact" stripped; only domain tags survive.
        assert "idea" not in action.tags
        assert "fact" not in action.tags
        assert "self-hosting" in action.tags
        assert "reverse-proxy" in action.tags

    @pytest.mark.asyncio
    async def test_tags_capped_at_five(
        self, vault_and_writer: tuple[Vault, GardenWriter], mock_llm: AsyncMock
    ) -> None:
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "T",
                    "content": "c",
                    "tags": [f"tag-{i}" for i in range(10)],
                    "entities": [],
                    "reason": "test",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = self._make_compiler(writer, mock_llm)
        result = await compiler.compile_batch(
            items=[BatchItem(label="x.md", content="hi")], seed_source="test"
        )
        assert len(result.actions_taken[0].tags) == 5

    @pytest.mark.asyncio
    async def test_hallucinated_entities_are_dropped(
        self, vault_and_writer: tuple[Vault, GardenWriter], mock_llm: AsyncMock
    ) -> None:
        """Entities that don't appear as [[wikilinks]] in content are dropped.

        The prompt mandates this; the cleaner enforces it. Without the
        guard, LLMs spray imagined connections that point to nothing."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        body = "Working with [[Vaultwarden]] and [[Caddy]] today."
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "Vaultwarden setup",
                    "content": body,
                    "tags": ["self-hosting"],
                    # Hallucinated [[OAuth]] never appears in body.
                    "entities": ["[[Vaultwarden]]", "[[Caddy]]", "[[OAuth]]", "RawName"],
                    "reason": "test",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        mock_llm.chat = AsyncMock(return_value=plan)
        compiler = self._make_compiler(writer, mock_llm)
        result = await compiler.compile_batch(
            items=[BatchItem(label="x.md", content="raw")], seed_source="test"
        )
        action = result.actions_taken[0]
        assert "[[Vaultwarden]]" in action.entities
        assert "[[Caddy]]" in action.entities
        # Hallucinated and malformed entries dropped.
        assert "[[OAuth]]" not in action.entities
        assert "RawName" not in action.entities

    @pytest.mark.asyncio
    async def test_robust_parse_strips_reasoning_prefix_and_fences(
        self, vault_and_writer: tuple[Vault, GardenWriter], mock_llm: AsyncMock
    ) -> None:
        """Even with suppress_reasoning=True, some providers leak a
        ``<think>...`` prefix or wrap the JSON in ```json fences. The
        parser pulls out the first ``[`` through last ``]`` regardless."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem

        _, writer = vault_and_writer
        raw = (
            "<think>I should produce a plan.</think>\n\n"
            "```json\n"
            '[{"action":"create","target_path":null,"title":"T","content":"c",'
            '"tags":["a"],"entities":[],"reason":"r","source_seeds":[1],"related":[]}]'
            "\n```\n\nThat's my plan."
        )
        mock_llm.chat = AsyncMock(return_value=raw)
        compiler = self._make_compiler(writer, mock_llm)
        result = await compiler.compile_batch(
            items=[BatchItem(label="x.md", content="hi")], seed_source="test"
        )
        assert result.notes_created == 1


class TestDeriveBatchCharBudget:
    """derive_batch_char_budget probes the model for its context window."""

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_probe_fails(self) -> None:
        from backend.knowledge.ingest.ingest_compiler import (
            _DEFAULT_BATCH_CHAR_BUDGET,
            derive_batch_char_budget,
        )

        # Unknown model + no api_base → both probes return None → fallback.
        budget = await derive_batch_char_budget(model="totally/made-up", api_base=None)
        assert budget == _DEFAULT_BATCH_CHAR_BUDGET

    @pytest.mark.asyncio
    async def test_uses_litellm_registry_for_known_model(self, monkeypatch) -> None:
        # Stub litellm.get_model_info so the test doesn't depend on the
        # registry shipping a particular model id.
        from backend.knowledge.ingest import ingest_compiler
        from backend.knowledge.ingest.ingest_compiler import (
            _DEFAULT_BATCH_CHAR_BUDGET,
            derive_batch_char_budget,
        )

        monkeypatch.setattr(
            ingest_compiler,
            "_litellm_max_input_tokens",
            lambda _model: 200_000,
        )
        budget = await derive_batch_char_budget(model="some/big-model", api_base=None)
        # 200k tokens × 3.5 chars × 0.4 safety = much larger than the
        # conservative local-LLM default.
        assert budget > _DEFAULT_BATCH_CHAR_BUDGET * 10

    @pytest.mark.asyncio
    async def test_ollama_budget_is_capped(self, monkeypatch) -> None:
        # Even when ollama declares a 200k+ token context, the local
        # generation cost makes huge prompts impractical — keep the
        # cap on so a small model doesn't get a stuffed prompt.
        from backend.knowledge.ingest import ingest_compiler

        monkeypatch.setattr(ingest_compiler, "_litellm_max_input_tokens", lambda _m: None)

        async def _stub_ctx(_model, _api):
            return 200_000  # huge declared context

        monkeypatch.setattr(ingest_compiler, "_ollama_context_length", _stub_ctx)
        budget = await ingest_compiler.derive_batch_char_budget(
            model="ollama_chat/glm-4.7-flash:latest",
            api_base="http://localhost:11434",
        )
        assert budget == ingest_compiler._OLLAMA_BUDGET_CAP

    @pytest.mark.asyncio
    async def test_probes_ollama_show_endpoint(self, monkeypatch) -> None:
        # Stub httpx.AsyncClient → simulate ollama returning a context length.
        from backend.knowledge.ingest import ingest_compiler

        class _StubResp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"model_info": {"glm.context_length": 32768}}

        class _StubClient:
            def __init__(self, *_a, **_k) -> None: ...
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a) -> None:
                return None

            async def post(self, *_a, **_k):
                return _StubResp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
        budget = await ingest_compiler.derive_batch_char_budget(
            model="ollama_chat/glm-4.7-flash:latest",
            api_base="http://localhost:11434",
        )
        # 32k tokens × 3.5 × 0.4 ≈ 45k, but ollama cap kicks in.
        assert budget == ingest_compiler._OLLAMA_BUDGET_CAP


class TestChunkBatchBudgetE14:
    """Lift E14 — the post-dogfood char-budget constants.

    Dogfood ran a bsvibe-app big-repo bootstrap (1134 chunks for 1377 file
    artifacts) and found 3.6% of chunks failed because the backend's
    per-caller timeout expired before ``opencode run`` on a single big
    file finished. Part B of the fix HALVES the budget constants so a big
    seed gets distributed across more, smaller chunks — each one bounded
    by the new 600 s caller timeout, with finer-grained progress.

    These tests pin the new constants AND assert behaviour: three seeds
    of varying size partition into more chunks than they did before, but
    not into so many that we lose batching entirely.
    """

    def test_default_budget_halved_to_2500(self) -> None:
        from backend.knowledge.ingest.ingest_compiler import _DEFAULT_BATCH_CHAR_BUDGET

        # 5_000 → 2_500 (Lift E14 halving).
        assert _DEFAULT_BATCH_CHAR_BUDGET == 2_500

    def test_ollama_cap_halved_to_4000(self) -> None:
        from backend.knowledge.ingest import ingest_compiler

        # 8_000 → 4_000 (Lift E14 halving).
        assert ingest_compiler._OLLAMA_BUDGET_CAP == 4_000

    def test_three_seeds_split_across_more_chunks_at_4000_budget(self) -> None:
        """Three small-to-medium seeds (1.5 KB / 3 KB / 5 KB) — small
        enough that NONE individually exceeds the legacy 8_000 cap, so
        the old budget would have packed them more aggressively. Halving
        to 4_000 forces strictly more chunks because the medium + large
        ones now exceed the budget per-chunk.

        Asserts relative behaviour (new > old) — the test stays
        meaningful if the constants are tuned further.
        """
        from backend.knowledge.ingest.ingest_compiler import BatchItem, _chunk_batch

        items = [
            BatchItem(label="small.py", content="A" * 1_500),
            BatchItem(label="medium.py", content="B" * 3_000),
            BatchItem(label="large.py", content="C" * 5_000),
        ]

        new_budget_chunks = _chunk_batch(items, 4_000)
        old_budget_chunks = _chunk_batch(items, 8_000)

        # Smaller budget => more chunks (more progress granularity).
        assert len(new_budget_chunks) > len(old_budget_chunks), (
            "halving the budget should produce strictly more chunks; "
            f"new={len(new_budget_chunks)} old={len(old_budget_chunks)}"
        )
        # Smoke that we're in the expected ballpark — 2 to 3 chunks at
        # 4_000-char budget given 1.5+3+5 KB seeds.
        assert 2 <= len(new_budget_chunks) <= 3

    def test_one_seed_per_chunk_when_each_exceeds_budget(self) -> None:
        """When every seed is bigger than the budget, the loop puts one
        truncated seed per chunk — no two items can share a chunk."""
        from backend.knowledge.ingest.ingest_compiler import BatchItem, _chunk_batch

        items = [BatchItem(label=f"f{i}.py", content="x" * 5_000) for i in range(3)]
        chunks = _chunk_batch(items, 2_500)
        assert len(chunks) == 3
        for chunk in chunks:
            assert len(chunk) == 1


class _ScriptedCompileLlm:
    """Deterministic CompileLlm seam — returns one canned plan string per call."""

    def __init__(self, plan_json: str) -> None:
        self._plan = plan_json
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        self.calls.append({"system": system, "messages": messages})
        return self._plan


class TestExtractEntityNames:
    """``extract_entity_names`` returns LLM-committed entity names (no garden write).

    This is the seam the settle→knowledge path uses to derive concepts from
    EXTRACTED ENTITIES (BSage's primary mechanism), replacing the summary
    word-tokenizer. Every name must survive the ``_clean_entities`` wikilink
    anti-hallucination gate.
    """

    def _compiler(self, tmp_path: Path, plan_json: str) -> Any:
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)
        return IngestCompiler(
            garden_writer=writer,
            llm_client=_ScriptedCompileLlm(plan_json),
            retriever=None,
            event_bus=None,
            max_updates=10,
        )

    @pytest.mark.asyncio
    async def test_returns_entity_names_from_wikilinks(self, tmp_path: Path) -> None:
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "Calculator in Python",
                    "content": "Built a [[calculator]] in [[Python]].",
                    "tags": ["math"],
                    "entities": ["[[calculator]]", "[[Python]]"],
                    "reason": "seed #1",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        compiler = self._compiler(tmp_path, plan)
        names = await compiler.extract_entity_names("Built a calculator in Python")
        assert names == ["calculator", "Python"]

    @pytest.mark.asyncio
    async def test_hallucinated_entities_are_dropped(self, tmp_path: Path) -> None:
        """An entity NOT present as a literal [[wikilink]] in content is dropped."""
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "T",
                    "content": "Discussed [[Python]] only.",
                    "tags": [],
                    "entities": ["[[Python]]", "[[Rust]]"],
                    "reason": "r",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        compiler = self._compiler(tmp_path, plan)
        names = await compiler.extract_entity_names("text")
        assert names == ["Python"]

    @pytest.mark.asyncio
    async def test_empty_text_returns_empty(self, tmp_path: Path) -> None:
        compiler = self._compiler(tmp_path, "[]")
        assert await compiler.extract_entity_names("   ") == []

    @pytest.mark.asyncio
    async def test_no_garden_note_is_written(self, tmp_path: Path) -> None:
        """Extraction must NOT write any garden note — it is read-only derivation."""
        plan = json.dumps(
            [
                {
                    "action": "create",
                    "target_path": None,
                    "title": "T",
                    "content": "About [[Python]].",
                    "tags": [],
                    "entities": ["[[Python]]"],
                    "reason": "r",
                    "source_seeds": [1],
                    "related": [],
                }
            ]
        )
        compiler = self._compiler(tmp_path, plan)
        await compiler.extract_entity_names("About Python")
        garden_dir = tmp_path / "garden"
        written = list(garden_dir.rglob("*.md")) if garden_dir.exists() else []
        assert written == []
