"""The :class:`IngestCompiler` core — orchestrates the per-chunk loop.

Lift L3 (v8 §17.6) leaves the *orchestration* here: chunk loop, event
emission, and the per-batch analytics seam. Everything the loop
dispatches to lives in a sibling module:

- :mod:`._chunking` — partition + budget probe.
- :mod:`._related_context` — per-chunk vault search.
- :mod:`._llm_compile` — LLM seam + prompt + parse + cleaning.
- :mod:`._actions` — plan execution + supporting data classes.

⚠️  CRITICAL invariant — guard with care across future edits:

The chunk loop in :meth:`IngestCompiler.compile_batch` calls
:func:`find_related` ONCE PER CHUNK, using THAT chunk's seeds as the
query. Do not hoist the call above the loop, do not cache the result
across chunks. The ``rag-batch-stale-related-context`` skill exists
because this exact bug shipped once already.

Lift E18 — chunks now fan out CONCURRENTLY (bounded by ``parallelism``)
so the backend uses the worker fleet's free capacity. The per-chunk
related-context lookup invariant is preserved: each parallel task still
calls ``_find_related`` with ITS chunk's seed query. The retriever
instance is shared across all chunks (single instance held on
``self._retriever``), so the underlying index is loaded once and reused
— a per-chunk SEARCH, not a per-chunk index rebuild. Event emissions
are serialized through an ``asyncio.Lock`` so the progress subscriber's
``chunks_done`` counter never races between concurrent chunks.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from backend.knowledge._internal.events import emit_event

from ._actions import (
    CompileResult,
    IngestBatchRecord,
    IngestBatchRecorder,
    UpdateAction,
    empty_compile_result,
    execute_plan,
    validate_action,
)
from ._chunking import (
    _DEFAULT_BATCH_CHAR_BUDGET,
    BatchItem,
    _chunk_batch,
    _truncate_item,
)
from ._llm_compile import (
    _WIKILINK_PATTERN,
    COMPILE_BATCH_SYSTEM_PROMPT,
    CompileLlm,
    LLMClient,
    build_user_message,
    clean_entities,
    parse_plan,
)
from ._related_context import find_related

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus
    from backend.knowledge.canonicalization.service import CanonicalizationService
    from backend.knowledge.graph.writer import GardenWriter
    from backend.knowledge.retrieval.retriever import VaultRetriever

logger = structlog.get_logger(__name__)


class IngestCompiler:
    """Compile seed content into garden notes at ingestion time."""

    def __init__(
        self,
        garden_writer: GardenWriter,
        llm_client: LLMClient,
        retriever: VaultRetriever | None = None,
        event_bus: EventBus | None = None,
        max_updates: int = 10,
        batch_char_budget: int | None = None,
        chunk_timeout_s: float | None = 300.0,
        canonicalization_service: CanonicalizationService | None = None,
        batch_recorder: IngestBatchRecorder | None = None,
        parallelism: int = 3,
    ) -> None:
        self._writer = garden_writer
        self._llm = llm_client
        self._retriever = retriever
        self._event_bus = event_bus
        self._max_updates = max_updates
        # Optional analytics seam — when wired, every batch emits one
        # ``ingest_batches`` row. ``None`` (the default) is a no-op so the
        # compiler stays usable without any DB session.
        self._batch_recorder = batch_recorder
        # ``None`` → conservative default; callers that know the model
        # (AppState construction) should pass a probed value.
        self._batch_char_budget = batch_char_budget or _DEFAULT_BATCH_CHAR_BUDGET
        # Per-chunk LLM timeout. Defaults to 300s so slow local LLMs
        # (qwen3:14b commonly takes 90-300s/call on consumer hardware)
        # finish without hitting the litellm 60s default and triggering
        # a retry loop. Set to ``None`` to use bsvibe-llm's default.
        self._chunk_timeout_s = chunk_timeout_s
        # Canonicalization service (Handoff §11). When wired, every cleaned
        # raw tag is run through the resolver before landing in the garden
        # note. Unresolved/ambiguous/blocked tags are dropped per spec.
        self._canon_service = canonicalization_service
        # Lift E18 — bound the number of in-flight chunks per ``compile_batch``.
        # Default matches the worker fleet's ``max_parallel_tasks=3``; operators
        # should size this ``<=`` total free worker slots so the backend's
        # capacity-aware dispatch (E16) is the constraint, not us. Must be >= 1.
        if parallelism < 1:
            raise ValueError(f"parallelism must be >= 1, got {parallelism}")
        self._parallelism = parallelism

    async def compile_batch(
        self,
        items: list[BatchItem],
        seed_source: str,
    ) -> CompileResult:
        """Compile multiple seeds with a single LLM plan.

        Plugins that import N files (ai-memory-input ZIP, chatgpt
        conversation export, etc.) call this once per import — the LLM
        sees every seed at once and produces a consolidated plan that
        can deduplicate, merge, and cross-reference across the batch.
        Cuts a 30-call import down to one (or a small number of
        chunks when the combined text exceeds ``_BATCH_CHAR_BUDGET``).
        """
        if not items:
            return empty_compile_result()

        start = time.perf_counter()
        chunks = _chunk_batch(items, self._batch_char_budget)
        await emit_event(
            self._event_bus,
            "INGEST_COMPILE_BATCH_START",
            {"source": seed_source, "item_count": len(items), "chunk_count": len(chunks)},
        )

        # Lift E18 — fan chunks out concurrently bounded by ``parallelism``.
        # The aggregation lock serializes shared-state mutation + event
        # emission so the progress subscriber's per-chunk counters stay
        # monotonic and per-chunk START→DONE/FAILED ordering is preserved.
        sem = asyncio.Semaphore(self._parallelism)
        actions_taken: list[UpdateAction] = []
        counters: dict[str, int] = {
            "notes_created": 0,
            "notes_updated": 0,
            "llm_calls": 0,
            "chunk_failures": 0,
        }
        agg_lock = asyncio.Lock()

        async def _process_chunk(chunk_index: int, chunk: list[BatchItem]) -> None:
            async with sem:
                async with agg_lock:
                    await emit_event(
                        self._event_bus,
                        "INGEST_COMPILE_BATCH_CHUNK_START",
                        {
                            "source": seed_source,
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunks),
                            "chunk_size": len(chunk),
                        },
                    )

                # ⚠️  PER-CHUNK related lookup (rag-batch-stale-related-context):
                # query the SHARED retriever instance with THIS chunk's seeds —
                # never hoist this call or cache its result across chunks.
                chunk_query = "\n\n".join(item.content[:500] for item in chunk)
                chunk_result: CompileResult | None = None
                try:
                    related_context = await self._find_related(chunk_query)
                    plan = await self._plan_batch_updates(chunk, seed_source, related_context)
                    chunk_result = await self._execute_plan(plan)
                except Exception:
                    # Per-chunk failure must NOT discard earlier chunks' work
                    # — log + emit FAILED and keep the gather going.
                    logger.warning(
                        "ingest_compile_chunk_failed",
                        source=seed_source,
                        chunk_index=chunk_index,
                        chunk_size=len(chunk),
                        exc_info=True,
                    )
                    async with agg_lock:
                        counters["chunk_failures"] += 1
                        await emit_event(
                            self._event_bus,
                            "INGEST_COMPILE_BATCH_CHUNK_FAILED",
                            {
                                "source": seed_source,
                                "chunk_index": chunk_index,
                                "chunk_count": len(chunks),
                            },
                        )
                    return

                async with agg_lock:
                    counters["llm_calls"] += 1
                    counters["notes_created"] += chunk_result.notes_created
                    counters["notes_updated"] += chunk_result.notes_updated
                    actions_taken.extend(chunk_result.actions_taken)
                    await emit_event(
                        self._event_bus,
                        "INGEST_COMPILE_BATCH_CHUNK_DONE",
                        {
                            "source": seed_source,
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunks),
                            "notes_created": chunk_result.notes_created,
                            "notes_updated": chunk_result.notes_updated,
                        },
                    )

        # Per-chunk exceptions are caught inside ``_process_chunk``; any
        # exception escaping here is structural (e.g. emit_event itself
        # failing) and SHOULD propagate — no silent swallow.
        await asyncio.gather(*(_process_chunk(i, c) for i, c in enumerate(chunks)))
        notes_created = counters["notes_created"]
        notes_updated = counters["notes_updated"]
        llm_calls = counters["llm_calls"]
        chunk_failures = counters["chunk_failures"]

        await emit_event(
            self._event_bus,
            "INGEST_COMPILE_BATCH_COMPLETE",
            {
                "source": seed_source,
                "item_count": len(items),
                "llm_calls": llm_calls,
                "notes_updated": notes_updated,
                "notes_created": notes_created,
            },
        )

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "ingest_compile_batch_complete",
            source=seed_source,
            items=len(items),
            llm_calls=llm_calls,
            updated=notes_updated,
            created=notes_created,
            chunk_failures=chunk_failures,
            elapsed_ms=elapsed_ms,
        )

        # Record the per-batch analytics row via the optional seam. Failures
        # here are swallowed: an analytics-row write must never turn a
        # successful ingest into an error (the notes are already on disk).
        await self._record_batch(
            IngestBatchRecord(
                seed_source=seed_source,
                seed_count=len(items),
                notes_created=notes_created,
                notes_updated=notes_updated,
                llm_calls=llm_calls,
                chunk_count=len(chunks),
                chunk_failures=chunk_failures,
                elapsed_ms=elapsed_ms,
            )
        )

        return CompileResult(
            actions_taken=actions_taken,
            notes_updated=notes_updated,
            notes_created=notes_created,
            llm_calls=llm_calls,
            seed_count=len(items),
            elapsed_ms=elapsed_ms,
            chunk_failures=chunk_failures,
        )

    async def extract_entity_names(self, text: str, *, label: str = "seed") -> list[str]:
        """Extract the entity NAMES the LLM commits to ``text`` — no garden write.

        Runs the SAME plan + anti-hallucination path :meth:`compile_batch` uses
        (``_plan_batch_updates`` → :func:`parse_plan` → :func:`clean_entities`) but
        stops short of writing any note: it returns only the de-duplicated entity
        names (the inner text of each surviving ``[[Name]]`` wikilink).

        This is the seam the settle→knowledge path uses to derive *concepts*
        from LLM-extracted entities (BSage's primary mechanism) instead of by
        tokenizing the work summary. Every returned name is guaranteed to have
        appeared as a literal ``[[Name]]`` in the LLM's own ``content`` — generic
        nouns ("work", "returns", "string") are excluded structurally, not by a
        denylist. Order follows first appearance across the plan; the caller is
        responsible for any further normalization / capping.

        Empty ``text`` (or a plan with no surviving entities) returns ``[]``.
        Errors propagate to the caller, which decides the fallback policy — the
        settle sink soft-falls back rather than break the settlement write.
        """
        if not text.strip():
            return []
        items = [_truncate_item(BatchItem(label=label, content=text), self._batch_char_budget)]
        related_context = await self._find_related(items[0].content[:500])
        plan = await self._plan_batch_updates(items, label, related_context)

        names: list[str] = []
        seen: set[str] = set()
        for raw_action in plan[: self._max_updates]:
            if not validate_action(raw_action):
                continue
            entities = clean_entities(raw_action.get("entities") or [], raw_action["content"])
            for wikilink in entities:
                match = _WIKILINK_PATTERN.match(wikilink.strip())
                if not match:
                    continue
                name = match.group(1).strip()
                if name and name not in seen:
                    names.append(name)
                    seen.add(name)
        return names

    async def _record_batch(self, record: IngestBatchRecord) -> None:
        """Best-effort persist of the ``ingest_batches`` analytics row."""
        if self._batch_recorder is None:
            return
        try:
            await self._batch_recorder.record(record)
        except Exception as exc:  # noqa: BLE001 — analytics must never break ingest
            logger.warning(
                "ingest_compile_batch_record_failed",
                source=record.seed_source,
                error=str(exc),
            )

    async def _find_related(self, seed_content: str) -> str:
        """Search vault for notes related to seed content.

        Thin instance-method wrapper around :func:`find_related` so tests
        that ``monkeypatch.object(compiler, "_find_related", ...)`` keep
        working. The invariant lives in :meth:`compile_batch`'s loop:
        call this once PER chunk with that chunk's seeds.
        """
        return await find_related(self._retriever, seed_content)

    async def _plan_batch_updates(
        self,
        items: list[BatchItem],
        seed_source: str,
        related_context: str,
    ) -> list[dict[str, Any]]:
        """Ask the LLM to plan updates covering an entire batch in one call."""
        user_msg = build_user_message(items, seed_source, related_context)
        raw = await self._llm.chat(
            system=COMPILE_BATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            suppress_reasoning=True,
            timeout_s=self._chunk_timeout_s,
        )
        return parse_plan(raw)

    def _parse_plan(self, raw: str) -> list[dict[str, Any]]:
        """Back-compat thin wrapper; new code should call :func:`parse_plan`."""
        return parse_plan(raw)

    def _validate_action(self, raw: dict[str, Any]) -> bool:
        """Back-compat thin wrapper; new code should call :func:`validate_action`."""
        return validate_action(raw)

    async def _execute_plan(self, plan: list[dict[str, Any]]) -> CompileResult:
        """Execute the planned actions, capped by ``max_updates``.

        Thin delegating wrapper around :func:`execute_plan` so existing
        unit tests that mock ``compiler._execute_plan`` keep working.
        """
        return await execute_plan(
            self._writer,
            self._canon_service,
            plan,
            self._max_updates,
        )


# Re-exported so the facade module can import these names from
# ``_compiler`` rather than from ``_llm_compile`` directly. Keeps the
# facade's wiring symmetrical and gives ruff a legitimate reason to
# leave the imports in place.
__all__ = [
    "CompileLlm",
    "IngestCompiler",
    "LLMClient",
]
