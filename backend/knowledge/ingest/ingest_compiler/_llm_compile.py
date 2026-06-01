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
