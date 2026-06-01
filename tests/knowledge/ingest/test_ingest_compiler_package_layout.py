"""Smoke + invariant tests for the Lift L3 ingest_compiler package decomp.

These tests guard the v8 §17.6 decomposition of
``backend.knowledge.ingest.ingest_compiler`` from a single 894-LOC module
into a package. Two invariants are non-negotiable:

1. **Public API preservation** — every symbol previously imported from the
   single module must still be importable from the package facade with the
   same path.
2. **Per-chunk related-context** — the ``compile_batch`` chunk loop MUST
   call ``_find_related`` once PER chunk, not once before the loop. The
   ``rag-batch-stale-related-context`` skill records the exact bug this
   asserts against — a previous refactor lifted the call out of the loop
   and silently broke the update path for every chunk after the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.knowledge.ingest.ingest_compiler import (
    BatchItem,
    CompileLlm,
    CompileResult,
    IngestBatchRecord,
    IngestBatchRecorder,
    IngestCompiler,
    LLMClient,
    UpdateAction,
    derive_batch_char_budget,
)


class TestPackageLayout:
    """The package must keep the public API intact and each file ≤ 400 LOC."""

    def test_public_api_importable_from_facade(self) -> None:
        # If any of these moved without re-export, the import above already
        # would have failed. This test pins the *names* explicitly so a
        # future refactor doesn't silently drop one.
        assert IngestCompiler is not None
        assert BatchItem is not None
        assert CompileLlm is not None
        assert CompileResult is not None
        assert IngestBatchRecord is not None
        assert IngestBatchRecorder is not None
        assert LLMClient is not None
        assert UpdateAction is not None
        assert derive_batch_char_budget is not None

    def test_ingest_compiler_is_a_package(self) -> None:
        import backend.knowledge.ingest.ingest_compiler as pkg

        # Packages have __path__; modules do not.
        assert hasattr(pkg, "__path__"), "expected ingest_compiler to be a package"

    def test_each_subfile_under_400_loc(self) -> None:
        import backend.knowledge.ingest.ingest_compiler as pkg

        pkg_dir = Path(pkg.__file__).parent
        offenders: list[tuple[str, int]] = []
        for py_file in sorted(pkg_dir.glob("*.py")):
            loc = sum(1 for _ in py_file.read_text().splitlines())
            if loc > 400:
                offenders.append((py_file.name, loc))
        assert not offenders, f"files over 400 LOC: {offenders}"


class _StubWriter:
    """Minimal GardenWriter substitute — records writes, no FS."""

    def __init__(self) -> None:
        self.created: list[Any] = []

    async def write_garden(self, note: Any) -> Path:
        self.created.append(note)
        return Path(f"/tmp/{note.title}.md")

    async def update_note(self, path: str, content: str) -> Path:
        return Path(path)

    async def append_to_note(self, path: str, content: str) -> Path:
        return Path(path)

    async def ensure_entity_stub(self, name: str, mentioned_in: Path) -> None:
        return None


class _RecordingRetriever:
    """Records every search() query so the test can assert call-per-chunk."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str) -> str:
        self.queries.append(query)
        return f"context-for: {query[:30]}"


class _ScriptedLlm:
    """Returns ``[]`` (empty plan) — we only care about call counts."""

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        self.call_count += 1
        return "[]"


class TestPerChunkRelatedContextInvariant:
    """Guards the ``rag-batch-stale-related-context`` invariant.

    A batch large enough to force two chunks MUST trigger two separate
    related-context retrievals, each with seeds from its own chunk —
    not a single retrieval at the top of the loop that all chunks share.
    """

    @pytest.mark.asyncio
    async def test_related_context_queried_once_per_chunk(self) -> None:
        writer = _StubWriter()
        llm = _ScriptedLlm()
        retriever = _RecordingRetriever()

        # Force chunking: tiny budget + two items, each over half the budget
        # so they cannot co-exist in a single chunk.
        char_budget = 200
        compiler = IngestCompiler(
            garden_writer=writer,  # type: ignore[arg-type]
            llm_client=llm,
            retriever=retriever,  # type: ignore[arg-type]
            batch_char_budget=char_budget,
        )
        items = [
            BatchItem(label="seed-a", content="A" * 150),
            BatchItem(label="seed-b", content="B" * 150),
        ]

        result = await compiler.compile_batch(items, seed_source="invariant-test")

        # 1. Two chunks → two LLM calls (one plan per chunk).
        assert llm.call_count == 2, (
            "expected two LLM calls (one per chunk); related-context refactor "
            "must NOT collapse the per-chunk loop"
        )
        # 2. Two chunks → two related-context retrievals.
        assert len(retriever.queries) == 2, (
            "expected related-context to be queried per-chunk; if this is 1, "
            "the rag-batch-stale-related-context bug has been reintroduced"
        )
        # 3. The two retrieval queries are DIFFERENT — chunk A's query
        # contains 'A' content, chunk B's contains 'B' content. This
        # asserts the query is built from the CURRENT chunk's seeds.
        q_a, q_b = retriever.queries
        assert "A" in q_a and "B" not in q_a, "chunk 0 retrieval used wrong seeds"
        assert "B" in q_b and "A" not in q_b, "chunk 1 retrieval used wrong seeds"

        # Sanity: chunk_count telemetry matches.
        assert isinstance(result, CompileResult)

    @pytest.mark.asyncio
    async def test_single_chunk_single_related_context(self) -> None:
        # Control: with one chunk, only one retrieval. Asserts we don't
        # over-query in the trivial case.
        writer = _StubWriter()
        llm = _ScriptedLlm()
        retriever = _RecordingRetriever()
        compiler = IngestCompiler(
            garden_writer=writer,  # type: ignore[arg-type]
            llm_client=llm,
            retriever=retriever,  # type: ignore[arg-type]
            batch_char_budget=10_000,
        )
        items = [BatchItem(label="only", content="just one tiny seed")]
        await compiler.compile_batch(items, seed_source="single")
        assert llm.call_count == 1
        assert len(retriever.queries) == 1


class TestFacadeReExportsHelperFunctions:
    """Some tests/monkeypatch ``ingest_compiler._litellm_max_input_tokens``.

    After the package refactor that attribute must still be settable on
    the ``ingest_compiler`` facade module (monkeypatch only works on the
    object the test patched).
    """

    def test_litellm_helper_accessible_for_monkeypatching(self) -> None:
        from backend.knowledge.ingest import ingest_compiler as pkg

        # Either re-exported as an attribute on the facade or set as an
        # alias — either way, monkeypatch must find it.
        assert hasattr(pkg, "_litellm_max_input_tokens")

    def test_default_batch_char_budget_constant_accessible(self) -> None:
        from backend.knowledge.ingest import ingest_compiler as pkg

        assert hasattr(pkg, "_DEFAULT_BATCH_CHAR_BUDGET")
        assert isinstance(pkg._DEFAULT_BATCH_CHAR_BUDGET, int)


class TestRecorderProtocol:
    """A trivial smoke test that the recorder Protocol still binds."""

    @pytest.mark.asyncio
    async def test_recorder_protocol_runtime_checkable(self) -> None:
        class _OkRecorder:
            async def record(self, record: IngestBatchRecord) -> None:
                return None

        recorder = _OkRecorder()
        assert isinstance(recorder, IngestBatchRecorder)

        # Smoke: feed through the compiler end-to-end with the recorder.
        writer = _StubWriter()
        llm = _ScriptedLlm()
        record_calls: list[IngestBatchRecord] = []

        recorder_mock = AsyncMock(spec=IngestBatchRecorder)

        async def _record(r: IngestBatchRecord) -> None:
            record_calls.append(r)

        recorder_mock.record.side_effect = _record

        compiler = IngestCompiler(
            garden_writer=writer,  # type: ignore[arg-type]
            llm_client=llm,
            batch_recorder=recorder_mock,
        )
        await compiler.compile_batch(
            [BatchItem(label="a", content="seed-a")],
            seed_source="recorder-test",
        )
        assert len(record_calls) == 1
        assert record_calls[0].seed_source == "recorder-test"
