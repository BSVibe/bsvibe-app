"""IngestCompiler — compile knowledge at ingestion time, not query time.

Inspired by Karpathy Wiki: when new data arrives, immediately find and
update/create related garden notes instead of waiting for scheduled skills.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import structlog

from backend.knowledge._internal.events import emit_event
from backend.knowledge.graph.writer import GardenNote

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus
    from backend.knowledge.canonicalization.service import CanonicalizationService
    from backend.knowledge.graph.writer import GardenWriter
    from backend.knowledge.retrieval.retriever import VaultRetriever

logger = structlog.get_logger(__name__)


@runtime_checkable
class CompileLlm(Protocol):
    """The single LLM dispatch seam :class:`IngestCompiler` depends on.

    The compile path asks the model for ONE structured plan (a JSON array
    of garden actions) per chunk — no tool calls, no multi-turn loop. This
    Protocol pins exactly that call shape, mirroring how
    :class:`backend.execution.orchestrator.LoopLlm` is the loop's one seam
    (one Protocol, never a Union of concretes — per the
    ``bsvibe-llm-wrapper-not-raw-litellm`` rule).

    Production backs this with a thin adapter over ``bsvibe_llm.LlmClient``
    / the GatewayDispatcher (wired by request-handler glue in a later
    chunk); tests inject a deterministic scripted extractor. Nothing here
    imports litellm or any concrete provider.

    ``suppress_reasoning`` asks reasoning models to skip the chain-of-thought
    preamble that would corrupt the JSON parse; ``timeout_s`` bounds a slow
    local-LLM call (``None`` defers to the backend default).
    """

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str: ...


# Replaces the ``bundle-k-integration`` ``LLMClient = Any`` stub: the
# compile path's LLM dependency is now the narrow :class:`CompileLlm`
# seam above. Kept as an alias so the constructor parameter name and any
# external annotations stay stable.
LLMClient = CompileLlm

# Conservative fallback when nothing better is known about the model
# (no probe, no override). Tuned for small local LLMs — large frontier
# models will set their own budget via :func:`derive_batch_char_budget`
# at AppState construction time. Single items larger than the budget
# are truncated rather than allowed to balloon the prompt.
_DEFAULT_BATCH_CHAR_BUDGET = 5_000

COMPILE_BATCH_SYSTEM_PROMPT = """\
You are an ingest compiler for a personal knowledge garden (Obsidian vault).

You receive:
1. A BATCH of NEW seeds (numbered) — recently captured raw notes.
2. EXISTING notes from the vault — context for deduplication.

Your job: produce a SINGLE consolidated plan as a JSON array of actions.

## Mental model

This is a digital garden, not a filing cabinet. Notes are connected by
[[wikilinks]] — every entity, concept, person, tool, or project mentioned
should be wikilinked. The graph emerges from connections, not from
categorization.

Do NOT classify notes into types. Do NOT invent a "type" tag like "idea",
"fact", "insight", "project". Tags describe what the content IS ABOUT
(domain, topic), not what KIND of note it is.

Identity in this graph comes from what a note connects to, not from what
folder or category we put it in.

## Output schema

Return a JSON array. Each action object has:

- "action": "create" | "update" | "append"
- "target_path": vault-relative path. Required for update/append. null for create.
- "title": short descriptive title (5-80 chars). No quotes around it.
- "content": markdown body. USE [[wikilinks]] liberally for any concept,
  person, tool, project, organization mentioned — even if the target note
  doesn't exist yet (the system auto-creates stubs).
- "tags": 2-5 free-form lowercase content tags (e.g. "authentication",
  "reverse-proxy", "cost-optimization"). Hyphen-separated. Avoid generic
  tags ("idea", "note", "thought") and kind tags ("fact", "insight").
- "entities": list of [[Name]] strings extracted from "content".
  Every item MUST appear as a [[wikilink]] in "content".
  Include people, products, concepts, tools, organizations, projects.
- "reason": one sentence stating why, citing seed numbers
  (e.g. "consolidates seeds #1, #4, #7").
- "source_seeds": list of integer seed numbers this action draws from.
- "related": list of EXISTING note titles (from the vault context) for
  cross-linking. Empty list if none apply.

## Rules

- Treat the entire batch as one body of incoming material — deduplicate
  across seeds, MERGE related items into one note when reasonable.
- Prefer UPDATE over CREATE when content meaningfully overlaps an
  existing note in the vault context.
- Every name in "entities" MUST appear as [[Name]] in "content". If you
  can't naturally fit it as a wikilink, drop it from entities.
- If a seed is too brief or has no extractable substance, omit it. Do
  not pad with filler content.
- Return [] if the entire batch warrants no action.
- Return ONLY the JSON array. No markdown code fences. No commentary
  before or after.

## Example

INPUT seeds:
SEED #1: "Tested Vaultwarden behind Caddy reverse proxy. The X-Forwarded-Proto header was the issue — without it, OAuth callbacks broke."
SEED #2: "Bitwarden client compatibility check for Vaultwarden — most clients work, except mobile push notifications need extra setup."

OUTPUT:
[
  {
    "action": "create",
    "target_path": null,
    "title": "Vaultwarden behind Caddy reverse proxy",
    "content": "Got [[Vaultwarden]] running behind [[Caddy]]. The trick was getting [[X-Forwarded-Proto]] right — without it, Vaultwarden assumed http and OAuth callbacks broke.\\n\\nClient compatibility: most [[Bitwarden]] clients work, except mobile push notifications which need additional setup.",
    "tags": ["self-hosting", "reverse-proxy", "bitwarden-compatibility"],
    "entities": ["[[Vaultwarden]]", "[[Caddy]]", "[[X-Forwarded-Proto]]", "[[Bitwarden]]"],
    "reason": "consolidates seeds #1 and #2, both about Vaultwarden self-hosting setup",
    "source_seeds": [1, 2],
    "related": []
  }
]
"""  # noqa: E501  -- prompt body has long natural-language lines on purpose


# Tags an LLM might emit even after the prompt says not to. Filtered at
# parse time — these are *kind* labels masquerading as content tags.
_KIND_TAG_BLOCKLIST: frozenset[str] = frozenset(
    {
        "idea",
        "ideas",
        "insight",
        "insights",
        "fact",
        "facts",
        "note",
        "notes",
        "thought",
        "thoughts",
        "project",
        "projects",
        "task",
        "tasks",
        "event",
        "events",
        "person",
        "people",
        "preference",
        "preferences",
    }
)
_MAX_TAGS_PER_ACTION: int = 5
_TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WIKILINK_PATTERN = re.compile(r"^\[\[(.+?)\]\]$")


@dataclass
class UpdateAction:
    """A single update/create/append action planned by the LLM."""

    action: Literal["update", "append", "create"]
    target_path: str | None
    title: str
    content: str
    reason: str
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class CompileResult:
    """Result of an ingest compilation."""

    actions_taken: list[UpdateAction]
    notes_updated: int
    notes_created: int
    seed_path: str = ""
    llm_calls: int = 1
    # Telemetry for the ``ingest_batches`` analytics row (see
    # :class:`backend.knowledge.ingest.db.IngestBatch`). ``seed_count`` is
    # the number of input :class:`BatchItem`s; ``elapsed_ms`` the wall-clock
    # cost of the whole batch compile. Populated by ``compile_batch``.
    seed_count: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True)
class IngestBatchRecord:
    """The data needed to persist one ``ingest_batches`` analytics row.

    Decouples :class:`IngestCompiler` from the DB: the compiler hands this
    plain record to an optional :class:`IngestBatchRecorder` seam, and the
    request-handler glue (a later chunk) backs that seam with a SQLAlchemy
    writer. Keeping the row write behind a Protocol means the compiler core
    imports no session machinery and stays unit-testable with a fake.
    """

    seed_source: str
    seed_count: int
    notes_created: int
    notes_updated: int
    llm_calls: int
    chunk_count: int
    chunk_failures: int
    elapsed_ms: int


@runtime_checkable
class IngestBatchRecorder(Protocol):
    """Persists an :class:`IngestBatchRecord` (the per-batch analytics row).

    Production binds this to a writer over
    :class:`backend.knowledge.ingest.db.IngestBatch` (workspace_id + region
    come from the same :class:`~backend.knowledge.factory.KnowledgeFactory`
    boundary that scoped the vault). ``None`` keeps the row write optional —
    a missing recorder must never break ingest.
    """

    async def record(self, record: IngestBatchRecord) -> None: ...


@dataclass
class BatchItem:
    """One labelled chunk fed to :meth:`IngestCompiler.compile_batch`.

    ``label`` is a human-readable identifier (e.g. filename) so the LLM
    can reference seeds in its reasoning. ``content`` is the raw seed
    text the LLM should consider.
    """

    label: str
    content: str


_REQUIRED_ACTION_FIELDS = {"action", "title", "content", "reason"}


def _empty_compile_result() -> CompileResult:
    return CompileResult(actions_taken=[], notes_updated=0, notes_created=0)


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
            return _empty_compile_result()

        start = time.perf_counter()
        chunks = _chunk_batch(items, self._batch_char_budget)
        await emit_event(
            self._event_bus,
            "INGEST_COMPILE_BATCH_START",
            {"source": seed_source, "item_count": len(items), "chunk_count": len(chunks)},
        )

        actions_taken: list[UpdateAction] = []
        notes_created = 0
        notes_updated = 0
        llm_calls = 0
        chunk_failures = 0

        for chunk_index, chunk in enumerate(chunks):
            # Per-chunk progress event so a long bulk import can stream
            # progress to a UI loading bar — see plugin runner / SSE
            # bridges. Plain payload (no exception details) so the event
            # bus stays free of large blobs.
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

            # Per-chunk related lookup — each chunk gets vault context
            # relevant to ITS own seeds, not items 1-3 of the whole
            # batch. Lets the LLM reuse / update existing notes
            # instead of always creating new ones.
            chunk_query = "\n\n".join(item.content[:500] for item in chunk)
            chunk_result: CompileResult | None = None
            try:
                related_context = await self._find_related(chunk_query)

                plan = await self._plan_batch_updates(chunk, seed_source, related_context)
                llm_calls += 1
                chunk_result = await self._execute_plan(plan)
            except Exception:
                # Per-chunk failure must NOT discard work that earlier
                # chunks already wrote to disk. Log and keep going so
                # bulk imports stay best-effort: a single malformed
                # batch shouldn't sink the whole compile.
                chunk_failures += 1
                logger.warning(
                    "ingest_compile_chunk_failed",
                    source=seed_source,
                    chunk_index=chunk_index,
                    chunk_size=len(chunk),
                    exc_info=True,
                )
                await emit_event(
                    self._event_bus,
                    "INGEST_COMPILE_BATCH_CHUNK_FAILED",
                    {
                        "source": seed_source,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                    },
                )
                continue
            actions_taken.extend(chunk_result.actions_taken)
            notes_created += chunk_result.notes_created
            notes_updated += chunk_result.notes_updated
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
        )

    async def extract_entity_names(self, text: str, *, label: str = "seed") -> list[str]:
        """Extract the entity NAMES the LLM commits to ``text`` — no garden write.

        Runs the SAME plan + anti-hallucination path :meth:`compile_batch` uses
        (``_plan_batch_updates`` → ``_parse_plan`` → :func:`_clean_entities`) but
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
            if not self._validate_action(raw_action):
                continue
            entities = _clean_entities(raw_action.get("entities") or [], raw_action["content"])
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
        """Search vault for notes related to seed content."""
        if self._retriever is None:
            return "No existing notes available."
        return await self._retriever.search(query=seed_content)

    async def _plan_batch_updates(
        self,
        items: list[BatchItem],
        seed_source: str,
        related_context: str,
    ) -> list[dict[str, Any]]:
        """Ask the LLM to plan updates covering an entire batch in one call."""
        seed_blocks = []
        for idx, item in enumerate(items, start=1):
            seed_blocks.append(f"### Seed #{idx} — {item.label}\n\n{item.content.strip()}\n")
        seeds_text = "\n".join(seed_blocks)
        user_msg = (
            f"## New Seeds (source: {seed_source}, count: {len(items)})\n\n"
            f"{seeds_text}\n\n"
            f"## Existing Related Notes\n\n{related_context}"
        )
        raw = await self._llm.chat(
            system=COMPILE_BATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            suppress_reasoning=True,
            timeout_s=self._chunk_timeout_s,
        )
        return self._parse_plan(raw)

    def _parse_plan(self, raw: str) -> list[dict[str, Any]]:
        """Parse LLM response as JSON array of actions.

        Robust against three known failure modes:
        - markdown code fences (```json ... ```)
        - reasoning-model preamble that survives suppression (e.g.
          stray ``<think>...`` even with ``thinking={"type": "disabled"}``)
        - trailing commentary after the array
        """
        text = raw.strip()
        # Pull out the first ``[`` through the matching last ``]`` —
        # everything outside is preamble/postamble we don't trust.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            logger.warning("ingest_compile_parse_no_array", raw=text[:200])
            return []
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            logger.warning("ingest_compile_parse_failed", raw=candidate[:200])
            return []
        if not isinstance(parsed, list):
            return []
        return parsed

    async def _execute_plan(self, plan: list[dict[str, Any]]) -> CompileResult:
        """Execute the planned actions, capped by max_updates."""
        actions_taken: list[UpdateAction] = []
        notes_created = 0
        notes_updated = 0

        for raw_action in plan[: self._max_updates]:
            if not self._validate_action(raw_action):
                continue

            tags = _clean_tags(raw_action.get("tags") or [])
            tags = await self._canonicalize_tags(tags, raw_source="ingest-compiler")
            entities = _clean_entities(raw_action.get("entities") or [], raw_action["content"])

            action = UpdateAction(
                action=raw_action["action"],
                target_path=raw_action.get("target_path"),
                title=raw_action["title"],
                content=raw_action["content"],
                reason=raw_action["reason"],
                tags=tags,
                entities=entities,
                related=raw_action.get("related", []),
            )

            try:
                if action.action == "create":
                    written_path = await self._writer.write_garden(
                        GardenNote(
                            title=action.title,
                            content=action.content,
                            source="ingest-compiler",
                            tags=action.tags,
                            entities=action.entities,
                            related=action.related,
                        )
                    )
                    notes_created += 1
                elif action.action == "update" and action.target_path:
                    written_path = await self._writer.update_note(
                        action.target_path, action.content
                    )
                    notes_updated += 1
                elif action.action == "append" and action.target_path:
                    written_path = await self._writer.append_to_note(
                        action.target_path, action.content
                    )
                    notes_updated += 1
                else:
                    logger.warning("ingest_compile_invalid_action", action=action.action)
                    continue
            except (FileNotFoundError, ValueError, OSError) as exc:
                logger.warning(
                    "ingest_compile_action_failed",
                    action=action.action,
                    title=action.title,
                    error=str(exc),
                )
                continue

            actions_taken.append(action)
            # Ensure every wikilink target has a real vault file so the
            # graph extractor's ``WIKILINK_RE`` sweep finds nodes on both
            # ends. Cleaned by ``_clean_entities`` already, so each item
            # is a valid ``[[Name]]`` actually present in the body.
            await self._ensure_entity_stubs(action.entities, written_path)

        return CompileResult(
            actions_taken=actions_taken,
            notes_updated=notes_updated,
            notes_created=notes_created,
        )

    async def _ensure_entity_stubs(self, entities: list[str], mentioned_in: Path | None) -> None:
        """Best-effort: create / refresh a stub for every ``[[Name]]`` mentioned.

        Failures are logged but never propagated — a single bad entity (e.g.
        slug that escapes vault boundary) must not abort the whole compile.
        """
        if not mentioned_in:
            return
        for wikilink in entities:
            match = _WIKILINK_PATTERN.match(wikilink.strip())
            if not match:
                continue
            name = match.group(1).strip()
            try:
                await self._writer.ensure_entity_stub(name, mentioned_in)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "ingest_compile_entity_stub_failed",
                    name=name,
                    error=str(exc),
                )

    async def _canonicalize_tags(self, tags: list[str], *, raw_source: str) -> list[str]:
        """Resolve cleaned tags to canonical concept ids (Handoff §11).

        Tags that resolve to existing concepts (or auto-create a new one)
        land in the garden note. Tags returning None
        (ambiguous/blocked/pending_candidate/auto-apply-failed) are dropped
        per spec — their evidence lives on the action/proposal record.
        """
        if self._canon_service is None or not tags:
            return tags
        canonical: list[str] = []
        seen: set[str] = set()
        for raw_tag in tags:
            try:
                resolved = await self._canon_service.resolve_and_canonicalize(
                    raw_tag, raw_source=raw_source
                )
            except Exception as exc:  # noqa: BLE001 — never abort ingest on resolve error
                logger.warning(
                    "ingest_compile_canonicalize_failed",
                    raw_tag=raw_tag,
                    error=str(exc),
                )
                continue
            if resolved is None or resolved in seen:
                continue
            canonical.append(resolved)
            seen.add(resolved)
        return canonical

    def _validate_action(self, raw: dict[str, Any]) -> bool:
        """Check that raw action dict has all required fields."""
        if not isinstance(raw, dict):
            return False
        missing = _REQUIRED_ACTION_FIELDS - raw.keys()
        if missing:
            logger.debug("ingest_compile_action_missing_fields", missing=list(missing))
            return False
        if raw["action"] not in ("create", "update", "append"):
            return False
        return not (raw["action"] in ("update", "append") and not raw.get("target_path"))


def _clean_tags(raw_tags: Any) -> list[str]:
    """Filter LLM-emitted tags to the documented contract.

    Drops kind tags ("idea", "fact"...) the prompt explicitly forbids,
    rejects values that don't match the lowercase-hyphen pattern, dedupes,
    and caps at ``_MAX_TAGS_PER_ACTION``.
    """
    if not isinstance(raw_tags, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        normalised = tag.strip().lower()
        if not normalised or normalised in seen:
            continue
        if normalised in _KIND_TAG_BLOCKLIST:
            continue
        if not _TAG_PATTERN.match(normalised):
            continue
        cleaned.append(normalised)
        seen.add(normalised)
        if len(cleaned) >= _MAX_TAGS_PER_ACTION:
            break
    return cleaned


def _clean_entities(raw_entities: Any, content: str) -> list[str]:
    """Drop entities that don't appear as ``[[wikilinks]]`` in ``content``.

    Anti-hallucination guard: the LLM is told every entity must also be
    in the body; we enforce it. Items that aren't in ``[[Name]]`` shape
    or whose target name doesn't appear inside any wikilink in ``content``
    are silently dropped.
    """
    if not isinstance(raw_entities, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for entity in raw_entities:
        if not isinstance(entity, str):
            continue
        match = _WIKILINK_PATTERN.match(entity.strip())
        if not match:
            continue
        canonical = f"[[{match.group(1).strip()}]]"
        if canonical in seen:
            continue
        # The exact wikilink (case-sensitive) must appear in the body —
        # otherwise the LLM invented it.
        if canonical not in content:
            continue
        cleaned.append(canonical)
        seen.add(canonical)
    return cleaned


def _chunk_batch(items: list[BatchItem], char_budget: int) -> list[list[BatchItem]]:
    """Split items into chunks whose total content stays under char_budget.

    Items larger than the budget are truncated (with a marker) so a
    single mega-file (e.g. an index page) can't blow up the prompt and
    starve the LLM. Order is preserved so seed numbering stays
    meaningful within each chunk.
    """
    chunks: list[list[BatchItem]] = []
    current: list[BatchItem] = []
    current_size = 0
    for raw_item in items:
        item = _truncate_item(raw_item, char_budget)
        item_size = len(item.content)
        if current and current_size + item_size > char_budget:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += item_size
    if current:
        chunks.append(current)
    return chunks


def _truncate_item(item: BatchItem, max_chars: int) -> BatchItem:
    """Cap an oversized item's content so it can fit in a single chunk."""
    if len(item.content) <= max_chars:
        return item
    head = item.content[: max_chars - 80]
    return BatchItem(
        label=item.label,
        content=f"{head}\n\n…[truncated for batch budget; original was {len(item.content)} chars]…",
    )


# Reserve this fraction of the model's input window for the system
# prompt, the per-batch frame text, and headroom for the JSON output —
# the rest gets allocated to seed payload.
_BUDGET_SAFETY_FRACTION = 0.4

# Rough chars-per-token for ASCII-heavy markdown. Korean/CJK runs ~2,
# but we're erring conservative on input length, not output.
_CHARS_PER_TOKEN = 3.5

# Local ollama models often DECLARE huge context windows (200k+) but
# actually generate slowly past a few thousand chars of input.
# Empirically, glm-4.7-flash on consumer GPU times out (>300s) at
# 16k input; 5-8k is the practical sweet spot. Cap their derived
# budget here — doesn't apply to hosted models (anthropic/openai/etc.)
# which handle long prompts efficiently.
_OLLAMA_BUDGET_CAP = 8_000


async def derive_batch_char_budget(
    model: str,
    api_base: str | None = None,
    *,
    fallback: int = _DEFAULT_BATCH_CHAR_BUDGET,
) -> int:
    """Probe the configured model for its context window, return a safe budget.

    Looks up the input-token limit (ollama via ``/api/show``, others via
    litellm's static model registry) and converts to a char budget that
    keeps room for the system prompt + LLM output. Falls back to
    ``fallback`` if probing fails.

    Computed once at AppState construction time and passed into
    :class:`IngestCompiler` — runtime model swaps trigger a re-probe at
    the same boundary.
    """
    max_input_tokens = await _probe_max_input_tokens(model, api_base)
    if max_input_tokens is None:
        logger.info("ingest_batch_budget_fallback", model=model, chars=fallback)
        return fallback
    budget = int(max_input_tokens * _CHARS_PER_TOKEN * _BUDGET_SAFETY_FRACTION)
    # Don't go below the conservative default — micro-models would just
    # produce thrashing chunks otherwise.
    budget = max(budget, _DEFAULT_BATCH_CHAR_BUDGET)
    # Local ollama models advertise huge contexts but generate slowly
    # past a few thousand input chars. Cap them so we don't ship a
    # technically-correct-but-practically-broken single 200k char
    # prompt to a small local model.
    if model.startswith(("ollama/", "ollama_chat/")):
        budget = min(budget, _OLLAMA_BUDGET_CAP)
    logger.info(
        "ingest_batch_budget_derived",
        model=model,
        max_input_tokens=max_input_tokens,
        chars=budget,
    )
    return budget


async def _probe_max_input_tokens(model: str, api_base: str | None) -> int | None:
    """Return max input tokens for the model, or ``None`` if unknown."""
    if model.startswith(("ollama/", "ollama_chat/")) and api_base:
        return await _ollama_context_length(model, api_base)
    return _litellm_max_input_tokens(model)


async def _ollama_context_length(model: str, api_base: str) -> int | None:
    """Ask ollama's ``/api/show`` for the model's declared context length."""
    bare_name = model.split("/", 1)[1] if "/" in model else model
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{api_base.rstrip('/')}/api/show", json={"name": bare_name})
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    info = data.get("model_info") or {}
    # ollama returns architecture-keyed entries like "glm.context_length".
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


def _litellm_max_input_tokens(model: str) -> int | None:
    """Look up the model in litellm's static registry."""
    try:
        import litellm

        info = litellm.get_model_info(model)
    except Exception:
        return None
    raw = info.get("max_input_tokens") if isinstance(info, dict) else None
    return raw if isinstance(raw, int) and raw > 0 else None
