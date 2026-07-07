"""Worth-remembering knowledge — the offline core.

Founder directive (2026-07): knowledge is NOT a work-history log. A verified run
only leaves a note when there is something worth REMEMBERING — a retrospective
insight, a non-obvious learning, or a user decision/choice. Routine work leaves
NOTHING.

v2: the WORKING AGENT declares what it learned in its verification contract
(``parse_declared_knowledge``); there is no post-hoc extractor. This module owns
the stack-agnostic core: the shape, the agent-declared parser, the tolerant dict
parser, the inherently-notable gate, and the shared bar the ingest compiler
embeds.
"""

from __future__ import annotations

from backend.knowledge.extraction.worth_remembering import (
    RememberableKnowledge,
    is_inherently_notable,
    parse_declared_knowledge,
    parse_extraction,
)

# ── parse_declared_knowledge — agent-authored knowledge from the contract ─────


def test_declared_knowledge_extracts_topic_and_insight() -> None:
    # v2: the working agent declares knowledge IN its verification contract.
    # Presence of a substantive knowledge block IS the signal (no separate flag).
    got = parse_declared_knowledge(
        {
            "checks": [{"kind": "command", "command": "pytest"}],
            "knowledge": {
                "topic": "Idempotent webhooks",
                "insight": "Dedupe webhook deliveries by event id — providers retry.",
            },
        }
    )
    assert got == RememberableKnowledge(
        topic="Idempotent webhooks",
        insight="Dedupe webhook deliveries by event id — providers retry.",
    )


def test_declared_knowledge_absent_is_none() -> None:
    # Routine work: the agent declares no knowledge block → nothing written.
    assert parse_declared_knowledge({"checks": [{"kind": "command", "command": "pytest"}]}) is None
    assert parse_declared_knowledge({}) is None
    assert parse_declared_knowledge(None) is None
    assert parse_declared_knowledge("not a dict") is None


def test_declared_knowledge_blank_fields_is_none() -> None:
    # A knowledge block with an empty topic or insight is not substantive → None.
    assert parse_declared_knowledge({"knowledge": {"topic": "", "insight": "x"}}) is None
    assert parse_declared_knowledge({"knowledge": {"topic": "X", "insight": "  "}}) is None
    assert parse_declared_knowledge({"knowledge": {}}) is None
    assert parse_declared_knowledge({"knowledge": "just a string"}) is None


def test_declared_knowledge_caps_topic_length() -> None:
    long_topic = "A very long knowledge name that rambles on well past the eighty character cap for a topic label"
    got = parse_declared_knowledge({"knowledge": {"topic": long_topic, "insight": "keep it"}})
    assert got is not None
    assert len(got.topic) <= 80


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


# ── shared bar — the ingest compiler embeds the one principle ────────────────


def test_ingest_prompt_embeds_the_shared_bar() -> None:
    """The ingest compiler (per imported file) embeds the SAME worth-remembering
    principle the agent-loop knowledge guidance surfaces — stated once, reused
    verbatim, so the two knowledge paths can't drift to different bars."""
    from backend.knowledge.extraction.worth_remembering import WORTH_REMEMBERING_PRINCIPLE
    from backend.knowledge.ingest.ingest_compiler._llm_compile import (
        COMPILE_BATCH_SYSTEM_PROMPT,
    )

    assert WORTH_REMEMBERING_PRINCIPLE in COMPILE_BATCH_SYSTEM_PROMPT
    # The shared bar names the exclusions that were the noise source.
    assert "NOT a work log" in WORTH_REMEMBERING_PRINCIPLE
    assert "keep nothing" in WORTH_REMEMBERING_PRINCIPLE.lower()
