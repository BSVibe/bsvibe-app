"""LLM seam + prompt + response-parsing for :mod:`ingest_compiler`.

Lift L3 (v8 §17.6) groups all LLM-facing concerns of the compile path
in one file: the narrow :class:`CompileLlm` Protocol, the system prompt,
the per-chunk user-message assembly, and the JSON-array parser plus the
post-parse hygiene helpers (``_clean_tags`` / ``_clean_entities``).

The :class:`CompileLlm` dispatch shape is deliberately a single Protocol
(not a Union of concretes) — same rule as
:class:`backend.execution.orchestrator.LoopLlm`. Tests inject a scripted
fake; production wires it to a thin adapter over ``bsvibe_llm.LlmClient``
(``bsvibe-llm-wrapper-not-raw-litellm``). Nothing here imports litellm.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.knowledge.extraction.worth_remembering import WORTH_REMEMBERING_PRINCIPLE

from ._chunking import BatchItem

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


# The import (per-file) and settle (per-run) paths hold the SAME bar — stated
# once in :data:`WORTH_REMEMBERING_PRINCIPLE` and embedded verbatim in both
# prompts so they cannot drift. The ingest prompt then adds its file-specific
# structure (the four note kinds, the JSON-array schema, the examples). Built by
# concatenation (NOT an f-string) because the schema/examples below carry literal
# ``{ }`` braces that an f-string would try to interpolate.
COMPILE_BATCH_SYSTEM_PROMPT = (
    "You are a knowledge garden curator. You receive code/docs from a project, and you must extract ONLY reusable engineering knowledge worth preserving across projects.\n\n"  # noqa: E501
    + WORTH_REMEMBERING_PRINCIPLE
    + """

⚠️ THIS IS NOT A FILE CATALOG. Do NOT create notes that describe files, classes, functions, or codebase structure. The source code itself is the source of truth for what code DOES. Notes are for what humans LEARN that they couldn't re-derive by re-reading the code.

For each chunk, produce 0-3 notes. Each note has a type:

1. **Pattern** — a recurring design or implementation pattern (e.g. "AsyncSession.flush() is not safe for concurrent use — race fires as 'Session is already flushing'; fix: per-call session via session_factory")
2. **Principle** — a non-obvious invariant or decision (e.g. "Workspace_id contextvar must be set before any session query for RLS to engage")
3. **TechInsight** — concrete library/protocol behavior (e.g. "opencode CLI's `run` subprocess pays ~8h startup tax per call; `serve` daemon + HTTP API is 3000x faster")
4. **DomainModel** — a system abstraction and its relations (e.g. "RunRoutingRule is matched by caller_id, then conditions evaluated against the run's framed signals")

If a chunk is just code that implements obvious things (CRUD endpoint, Pydantic model, simple FastAPI route, boilerplate `__init__.py`), produce **0 notes**. Most code is uninteresting — that's the expected outcome.

## Output schema (JSON array)

Each note object:
- "action": "create" | "update" (use update if vault context contains a meaningfully overlapping note)
- "type": "Pattern" | "Principle" | "TechInsight" | "DomainModel" (REQUIRED — these four only)
- "target_path": vault-relative path. Required for update. null for create.
- "title": short descriptive title (5-80 chars)
- "content": 2-6 sentences of insight. Plain markdown. Wikilinks ONLY for entities worth their own future note (major technologies, products, named concepts) — NEVER for function names, file paths, variable names, codebase-internal identifiers.
- "wikilinks": list of `[[X]]` strings — MUST be a strict subset of "content"
- "tags": 2-4 lowercase hyphen-separated content tags ("async-cancellation", "rate-limiting"). Avoid kind tags ("idea", "concept").
- "source_chunk_index": integer (0-based)
- "reason": one sentence why this is worth preserving across projects.

Return ONLY the JSON array. No code fences. No commentary.

## Examples

Good note (from a real BSVibe dogfood finding):
{
  "action": "create",
  "type": "Pattern",
  "title": "Per-call AsyncSession to escape concurrent flush race",
  "content": "[[SQLAlchemy]] AsyncSession is NOT safe for concurrent use — two parallel asyncio tasks calling `session.flush()` on the same session race and one raises `InvalidRequestError: Session is already flushing`. Fix: wire an `async_sessionmaker[AsyncSession]` factory through the call chain; each parallel branch opens its own session for its writes. Lock-based serialization defeats the parallelism — factory is the right answer.",
  "wikilinks": ["[[SQLAlchemy]]"],
  "tags": ["async-concurrency", "database-session", "race-condition"],
  "source_chunk_index": 12,
  "reason": "Future projects using SQLAlchemy AsyncSession with asyncio.gather will hit the same race; this principle is repo-independent."
}

Bad note (DO NOT produce):
{
  "action": "create",
  "title": "backend/__init__.py uses __all__ markers",
  "content": "The BSVibe backend root package is namespace-only. Public surface lives under six bounded contexts: router, knowledge, workflow, identity, schedule, extensions. Lift N (v8 §22) adds __all__ markers across every backend sub-package.",
  ...
}
This is a codebase description. Not reusable. Return [] for this chunk.

Return [] when the chunk has no insight worth extracting. THIS IS THE COMMON CASE.
"""
)  # noqa: E501  -- prompt body has long natural-language lines on purpose


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


def build_user_message(
    items: list[BatchItem],
    seed_source: str,
    related_context: str,
) -> str:
    """Assemble the per-chunk user message: seeds + vault context.

    ``related_context`` is the per-chunk retrieval result. The CALLER is
    responsible for computing it inside the chunk loop — never hoist that
    call up here. See :mod:`._related_context` for the invariant.
    """
    seed_blocks: list[str] = []
    for idx, item in enumerate(items, start=1):
        seed_blocks.append(f"### Seed #{idx} — {item.label}\n\n{item.content.strip()}\n")
    seeds_text = "\n".join(seed_blocks)
    return (
        f"## New Seeds (source: {seed_source}, count: {len(items)})\n\n"
        f"{seeds_text}\n\n"
        f"## Existing Related Notes\n\n{related_context}"
    )


def parse_plan(raw: str) -> list[dict[str, Any]]:
    """Parse LLM response as JSON array of actions.

    Robust against three known failure modes:
    - markdown code fences (```json ... ```)
    - reasoning-model preamble that survives suppression (e.g.
      stray ``<think>...`` even with ``thinking={"type": "disabled"}``)
    - trailing commentary after the array

    Lift E20 — an EMPTY array (``[]``) is the prompt's documented "no
    insight in this chunk" signal. Logged at ``info`` as
    ``ingest_chunk_no_insight`` (not a warning); the chunk loop just
    returns 0 actions and moves on. Real parse failures still log
    ``ingest_compile_parse_*`` at warning level.
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
    if not parsed:
        logger.info("ingest_chunk_no_insight")
    return parsed


def clean_tags(raw_tags: Any) -> list[str]:
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


def clean_entities(raw_entities: Any, content: str) -> list[str]:
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
