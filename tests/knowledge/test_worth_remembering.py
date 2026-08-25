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


def test_declared_knowledge_humanizes_slug_topic() -> None:
    # Agents sometimes emit a kebab/snake slug as the topic (observed live in EN:
    # 'bsvibe-sandbox-editable-install-not-reliable'). The chip must read like a
    # human knowledge NAME, so a slug (no spaces + separators) is de-slugged.
    got = parse_declared_knowledge(
        {"knowledge": {"topic": "auth-loopback-redirect", "insight": "must string-match"}}
    )
    assert got is not None
    assert got.topic == "Auth loopback redirect"

    snake = parse_declared_knowledge(
        {"knowledge": {"topic": "pytest_pythonpath_for_src_layout", "insight": "x"}}
    )
    assert snake is not None
    assert snake.topic == "Pytest pythonpath for src layout"


def test_declared_knowledge_leaves_real_phrase_untouched() -> None:
    # A topic that already reads as a phrase (has whitespace) is NOT altered —
    # incl. legitimately hyphenated terms inside a phrase.
    got = parse_declared_knowledge(
        {"knowledge": {"topic": "Copy-on-write semantics", "insight": "x"}}
    )
    assert got is not None
    assert got.topic == "Copy-on-write semantics"
    # A single word (no separators) is left as-is.
    one = parse_declared_knowledge({"knowledge": {"topic": "Idempotency", "insight": "x"}})
    assert one is not None
    assert one.topic == "Idempotency"


# ── is_inherently_notable — some settlements are always worth keeping ─────────


def test_user_decision_is_inherently_notable() -> None:
    # A resolved checkpoint is a USER CHOICE — worth remembering with no LLM
    # judgement needed, PROVIDED the founder actually wrote the answer.
    assert is_inherently_notable("decision_resolution", founder_text="Postgres") is True


def test_negative_pattern_is_inherently_notable() -> None:
    # A discard-with-REASON is a LEARNING — the reason is the founder's own text.
    assert is_inherently_notable("negative_pattern", founder_text="hard to reason about") is True


def test_plain_verified_work_is_not_inherently_notable() -> None:
    # Routine verified work is NOT automatically notable — it must earn a note via
    # the agent's own declaration (and routine utility work earns nothing).
    assert is_inherently_notable(None, founder_text="anything") is False
    assert is_inherently_notable("verified_work", founder_text="anything") is False


def test_a_notable_kind_without_founder_text_is_not_notable() -> None:
    """§13 — the precondition the docstring always named. A one-click action's
    ``answer`` is the BUTTON KEY, so it carries zero founder characters and the
    kind alone must not grant a note. The end-to-end proof (producer → sink) and
    the two positive controls live in
    ``tests/knowledge/test_settlement_needs_founder_text.py``."""
    assert is_inherently_notable("decision_resolution", founder_text=None) is False
    assert is_inherently_notable("decision_resolution", founder_text="   ") is False
    assert is_inherently_notable("negative_pattern", founder_text=None) is False


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
