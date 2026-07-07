"""Worth-remembering knowledge — the offline core.

Founder directive (2026-07): knowledge is NOT a work-history log. A run only
leaves a note when there is something worth REMEMBERING — a retrospective
insight, a non-obvious learning, or a user decision/choice. Routine work leaves
nothing.

v2: the WORKING AGENT records what it learned IN THE MOMENT (full working
context) by declaring an optional ``knowledge`` block in its verification
contract — there is NO post-hoc extractor, because a settle-time reader can't
see the tacit knowledge (the dead-ends, the constraint hit mid-work) that never
lands in the diff.

This module is pure + offline: the shape (:class:`RememberableKnowledge`), the
parse of the agent's declared block (:func:`parse_declared_knowledge`), the
tolerant dict parser (:func:`parse_extraction`), the "inherently notable" gate
(a user decision / a discard-with-reason is always kept), and the shared bar
(:data:`WORTH_REMEMBERING_PRINCIPLE`) the ingest compiler embeds + the executor
knowledge-declaration guidance surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The topic is a short KNOWLEDGE NAME (a noun-phrase-ish label), never a task
#: sentence or a file path — this is what the "추가한 지식" chip renders.
MAX_TOPIC_CHARS = 80
MAX_INSIGHT_CHARS = 2000

#: Settlement kinds that are ALWAYS worth remembering, with no LLM judgement:
#: a resolved checkpoint is a user CHOICE, a discard-with-reason is a LEARNING.
_INHERENTLY_NOTABLE_KINDS: frozenset[str] = frozenset({"decision_resolution", "negative_pattern"})


@dataclass(frozen=True)
class RememberableKnowledge:
    """One thing worth remembering: a short ``topic`` (the knowledge NAME) and
    the ``insight`` body (the learning / decision / observation to keep)."""

    topic: str
    insight: str


def is_inherently_notable(kind: str | None) -> bool:
    """True when a settlement is worth keeping regardless of the LLM verdict — a
    user decision or a discard-with-reason is knowledge by construction. Plain
    verified work (``None`` / ``"verified_work"``) is NOT: it must earn a note
    through :func:`parse_extraction`, and routine work earns none."""
    return kind in _INHERENTLY_NOTABLE_KINDS


def parse_extraction(raw: Any) -> RememberableKnowledge | None:
    """Parse an LLM worth-remembering verdict. Tolerant, and biased to ``None``:
    the default outcome for routine work is "nothing to keep", so anything that
    isn't an explicit, substantive memory yields ``None`` (no note written).

    Accepts a couple of key aliases (``remember``/``title``/``note``). ``None``
    when: not a dict, ``worth_remembering`` is false/absent, or the topic/insight
    are blank. The topic is trimmed + capped to a short label."""
    if not isinstance(raw, dict):
        return None
    flag = raw.get("worth_remembering")
    if flag is None:
        flag = raw.get("remember")
    if not bool(flag):
        return None
    topic = str(raw.get("topic") or raw.get("title") or "").strip()
    insight = str(raw.get("insight") or raw.get("note") or raw.get("body") or "").strip()
    if not topic or not insight:
        return None
    return RememberableKnowledge(
        topic=topic[:MAX_TOPIC_CHARS].rstrip(),
        insight=insight[:MAX_INSIGHT_CHARS].rstrip(),
    )


def parse_declared_knowledge(contract: Any) -> RememberableKnowledge | None:
    """Parse the knowledge the WORKING AGENT declared in its verification contract.

    v2 (founder directive): the agent that did the work records what it learned
    IN THE MOMENT — with the full working context a post-hoc extractor never has
    — as an optional ``knowledge`` block inside the ``<verification-contract>``.
    The PRESENCE of a substantive block is the signal (no ``worth_remembering``
    flag needed): routine work declares no block → nothing written.

    ``None`` when: not a dict, no ``knowledge`` key, the block isn't a dict, or
    its topic/insight are blank. The topic is trimmed + capped to a short label."""
    if not isinstance(contract, dict):
        return None
    block = contract.get("knowledge")
    if not isinstance(block, dict):
        return None
    topic = str(block.get("topic") or "").strip()
    insight = str(block.get("insight") or block.get("note") or block.get("body") or "").strip()
    if not topic or not insight:
        return None
    return RememberableKnowledge(
        topic=topic[:MAX_TOPIC_CHARS].rstrip(),
        insight=insight[:MAX_INSIGHT_CHARS].rstrip(),
    )


#: The ONE bar both knowledge-writing paths hold — the settle sink (per verified
#: run, via the AGENT's own contract declaration) and the ingest compiler (per
#: imported file). Stated once here so the guidance can't drift apart: knowledge
#: is not a work/file log, keep only what is worth remembering, default to
#: keeping nothing. The ingest compiler embeds this in its system prompt; the
#: agent-loop path surfaces it in the executor's knowledge-declaration guidance.
WORTH_REMEMBERING_PRINCIPLE = (
    "Knowledge is NOT a work log or a file catalog. Keep ONLY what is worth "
    "REMEMBERING — a reusable INSIGHT, a non-obvious LEARNING (a gotcha, a "
    "constraint discovered, why one approach was chosen over another), or a "
    "DECISION future work must honour. Routine or boilerplate work leaves "
    "NOTHING behind; the default outcome is to keep nothing."
)


__all__ = [
    "MAX_INSIGHT_CHARS",
    "MAX_TOPIC_CHARS",
    "RememberableKnowledge",
    "WORTH_REMEMBERING_PRINCIPLE",
    "is_inherently_notable",
    "parse_declared_knowledge",
    "parse_extraction",
]
