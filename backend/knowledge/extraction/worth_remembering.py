"""Worth-remembering knowledge extraction — the offline core.

Founder directive (2026-07): knowledge is NOT a work-history log. The old settle
path deposited a mechanical "this run added gcd()" seedling for EVERY verified
run, titled by the raw Direction — so the "추가한 지식" surface read like a task
list, and the concept graph filled with noise. New rule: a run/file only leaves
a note when there is something worth REMEMBERING — a retrospective insight, a
non-obvious learning, or a user decision/choice. Routine work leaves nothing.

This module is pure + offline: the shape of an extracted memory, the parse of an
LLM verdict into ``RememberableKnowledge | None``, the "inherently notable" gate
(a user decision / a discard-with-reason is always worth keeping — no LLM needed),
and the extractor prompt. The LLM call itself lives in the settle sink / ingest
compiler, which share this core so both paths hold the same bar.
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


_SYSTEM_PROMPT = (
    "You decide whether a completed unit of work left behind anything WORTH "
    "REMEMBERING, and if so, name it. Knowledge is NOT a work log: do NOT record "
    "that a routine task happened (adding a utility, fixing a typo, writing "
    "tests). Keep ONLY: a retrospective INSIGHT, a non-obvious LEARNING (a gotcha, "
    "a constraint discovered, why an approach was chosen over another), or a user "
    "DECISION / choice that future work should honour.\n"
    "Output ONLY a JSON object:\n"
    '  {"worth_remembering": true|false, "topic": "<short knowledge name>", '
    '"insight": "<the thing to remember, 1-3 sentences>"}\n'
    "RULES:\n"
    "- Default to false. Most routine work is NOT worth remembering — return "
    '{"worth_remembering": false} and nothing else.\n'
    "- ``topic`` is a SHORT noun-phrase knowledge NAME (e.g. 'Idempotent "
    "webhooks', 'Auth loopback redirect'), never a task sentence or a file path.\n"
    "- ``insight`` states WHAT to remember and WHY it matters — not what was done.\n"
    "- No prose outside the JSON object."
)


def worth_remembering_messages(
    *, intent: str | None, summary: str | None, diff: str | None = None
) -> list[dict[str, str]]:
    """Build the extractor chat messages. The system turn spells out the bar
    (keep insights/learnings/decisions, drop routine work); the user turn carries
    the work context (the founder's intent + what changed)."""
    parts: list[str] = []
    if intent and intent.strip():
        parts.append(f"The founder asked for:\n{intent.strip()}")
    if summary and summary.strip():
        parts.append(f"What changed:\n{summary.strip()[:1500]}")
    if diff and diff.strip():
        parts.append(f"The change:\n{diff.strip()[:4000]}")
    user = "\n\n".join(parts) if parts else "(no work context)"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = [
    "MAX_INSIGHT_CHARS",
    "MAX_TOPIC_CHARS",
    "RememberableKnowledge",
    "is_inherently_notable",
    "parse_extraction",
    "worth_remembering_messages",
]
