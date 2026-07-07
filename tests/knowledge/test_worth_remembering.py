"""Worth-remembering knowledge extraction (settle + ingest share this).

Founder directive (2026-07): knowledge is NOT a work-history log. A verified run
must NOT deposit a mechanical "this run added gcd()" seedling. Knowledge is only
what is worth REMEMBERING — a retrospective insight, a non-obvious learning, or a
user decision/choice. Routine work (add a utility, fix a typo) leaves NOTHING.

This module owns the stack-agnostic, offline core: the parse of an LLM verdict
into ``RememberableKnowledge | None``, the "is this settlement inherently
notable" gate (a user decision / a discard-with-reason is always worth keeping),
and the extractor prompt. The LLM call lives in the sink/compiler.
"""

from __future__ import annotations

import pytest

from backend.knowledge.extraction.worth_remembering import (
    RememberableKnowledge,
    is_inherently_notable,
    parse_extraction,
    parse_verdict_text,
    worth_remembering_messages,
)


# ── parse_extraction — LLM verdict → RememberableKnowledge | None ─────────────


def test_parse_returns_none_when_nothing_worth_remembering() -> None:
    # The routine case: the model judges there is nothing to keep. The canonical
    # shape is an explicit flag; the whole point is that routine work deposits
    # NOTHING, so a false/empty verdict must yield None (no note written).
    assert parse_extraction({"worth_remembering": False}) is None
    assert parse_extraction({"worth_remembering": False, "topic": "", "insight": ""}) is None


def test_parse_returns_none_on_garbage_or_empty() -> None:
    assert parse_extraction(None) is None
    assert parse_extraction({}) is None
    assert parse_extraction("not a dict") is None
    # worth_remembering true but no substance → nothing to write → None.
    assert parse_extraction({"worth_remembering": True, "topic": "  ", "insight": ""}) is None


def test_parse_extracts_topic_and_insight() -> None:
    out = parse_extraction(
        {
            "worth_remembering": True,
            "topic": "Idempotent retries",
            "insight": "The webhook must dedupe on delivery id — the provider re-sends within 5s.",
        }
    )
    assert isinstance(out, RememberableKnowledge)
    assert out.topic == "Idempotent retries"
    assert "dedupe on delivery id" in out.insight


def test_parse_tolerates_aliases_and_trims() -> None:
    # Tolerant like the other LLM-output parsers: accept a couple of key aliases
    # and trim whitespace so a slightly-off response still lands.
    out = parse_extraction({"remember": True, "title": "Auth loopback", "note": "  keep it  "})
    assert out is not None
    assert out.topic == "Auth loopback"
    assert out.insight == "keep it"


def test_topic_is_a_knowledge_name_not_a_task_sentence() -> None:
    # The topic is capped to a short noun-phrase-ish label so the "추가한 지식"
    # chip reads like a KNOWLEDGE NAME, never a task sentence / file path.
    long_topic = "Add a slugify utility to src/toolkit/strings.py that turns a string into a slug"
    out = parse_extraction({"worth_remembering": True, "topic": long_topic, "insight": "x"})
    assert out is not None
    assert len(out.topic) <= 80


# ── is_inherently_notable — some settlements are always worth keeping ─────────


def test_user_decision_is_inherently_notable() -> None:
    # A resolved checkpoint is a USER CHOICE — always worth remembering, no LLM
    # judgement needed (the founder decided something).
    assert is_inherently_notable("decision_resolution") is True


def test_negative_pattern_is_inherently_notable() -> None:
    # A discard-with-reason is a LEARNING — always worth keeping.
    assert is_inherently_notable("negative_pattern") is True


def test_plain_verified_work_is_not_inherently_notable() -> None:
    # Routine verified work is NOT automatically notable — it must earn a note via
    # the extractor (and routine utility work earns nothing).
    assert is_inherently_notable(None) is False
    assert is_inherently_notable("verified_work") is False


# ── parse_verdict_text — raw LLM text → RememberableKnowledge | None ─────────


def test_parse_verdict_text_plain_json() -> None:
    got = parse_verdict_text(
        '{"worth_remembering": true, "topic": "Idempotent webhooks", '
        '"insight": "Dedupe by event id."}'
    )
    assert got == RememberableKnowledge(topic="Idempotent webhooks", insight="Dedupe by event id.")


def test_parse_verdict_text_strips_code_fence_and_preamble() -> None:
    raw = (
        "Sure, here is the verdict:\n```json\n"
        '{"worth_remembering": true, "topic": "Auth loopback", "insight": "redirect_uri must match."}\n'
        "```\nHope that helps!"
    )
    got = parse_verdict_text(raw)
    assert got is not None
    assert got.topic == "Auth loopback"


def test_parse_verdict_text_routine_false_is_none() -> None:
    assert parse_verdict_text('{"worth_remembering": false}') is None


def test_parse_verdict_text_garbage_is_none() -> None:
    assert parse_verdict_text("no json here at all") is None
    assert parse_verdict_text("") is None
    assert parse_verdict_text(None) is None


# ── prompt — the "worth remembering" bar is spelled out ──────────────────────


def test_prompt_defines_the_worth_remembering_bar() -> None:
    messages = worth_remembering_messages(
        intent="Add a gcd(a, b) utility", summary="src/toolkit/mathx.py", diff="+def gcd(...)"
    )
    system = next(m["content"] for m in messages if m["role"] == "system")
    # It must tell the model: keep insights/learnings/decisions, NOT routine work.
    assert "worth_remembering" in system
    assert "routine" in system.lower()
    lowered = system.lower()
    assert "insight" in lowered or "learn" in lowered
    # And the work context is in the user turn.
    user = next(m["content"] for m in messages if m["role"] == "user")
    assert "gcd" in user
