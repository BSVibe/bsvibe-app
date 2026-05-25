"""Unit tests for the executor context-assembly helpers (B8).

B8 brings the CLI-worker dispatch up to native parity: instead of shipping a
bare 512-char intent with an EMPTY system prompt, the orchestrator now frames a
context-rich prompt (intent + relevant canon + founder-resolved decisions) and
passes a real engineer system prompt to ``create_task(system=...)``.

These exercise the pure assembly helpers in isolation — no DB, no redis, no
sandbox — so the framing logic + caps are covered without the full dispatch.
"""

from __future__ import annotations

import uuid

from backend.execution.db import ExecutionRun, RunStatus
from backend.executors.orchestrator import (
    _EXECUTOR_SYSTEM_PROMPT,
    _INTENT_MAX_CHARS,
    _KNOWLEDGE_MAX_CHARS_PER_STATEMENT,
    _KNOWLEDGE_MAX_RESULTS,
    _assemble_executor_prompt,
    _executor_system_prompt,
    _resolved_decisions,
)


def _run(payload: dict[str, object]) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload=payload,
    )


def test_system_prompt_is_non_empty() -> None:
    prompt = _executor_system_prompt()
    assert prompt
    assert prompt == _EXECUTOR_SYSTEM_PROMPT
    # It is engineer guidance for a delegated CLI agent: produce the artifacts.
    lower = prompt.lower()
    assert "engineer" in lower


def test_intent_only_when_no_canon_no_decisions() -> None:
    run = _run({"intent_text": "build the widget"})
    framed = _assemble_executor_prompt(run, statements=[])
    assert "build the widget" in framed
    # No empty section headers when there is nothing to fold in.
    assert "Relevant established patterns" not in framed
    assert "The founder resolved" not in framed


def test_intent_plus_canon() -> None:
    run = _run({"intent_text": "build the widget"})
    framed = _assemble_executor_prompt(
        run, statements=["prefer composition over inheritance", "tests live in tests/"]
    )
    assert "build the widget" in framed
    assert "Relevant established patterns" in framed
    assert "prefer composition over inheritance" in framed
    assert "tests live in tests/" in framed
    # No decisions section when none are resolved.
    assert "The founder resolved" not in framed


def test_intent_plus_canon_plus_decisions() -> None:
    run = _run(
        {
            "intent_text": "build the widget",
            "resolved_decisions": [
                {"decision_id": "d1", "question": "Use TS?", "answer": "Yes, TypeScript"},
            ],
        }
    )
    framed = _assemble_executor_prompt(run, statements=["tests live in tests/"])
    assert "build the widget" in framed
    assert "Relevant established patterns" in framed
    assert "tests live in tests/" in framed
    assert "The founder resolved" in framed
    assert "Use TS?" in framed
    assert "Yes, TypeScript" in framed


def test_decisions_without_answer_are_skipped() -> None:
    run = _run(
        {
            "intent_text": "x",
            "resolved_decisions": [
                {"question": "answered?", "answer": ""},
                {"question": "real?", "answer": "yes"},
            ],
        }
    )
    framed = _assemble_executor_prompt(run, statements=[])
    assert "real?" in framed
    assert "answered?" not in framed


def test_canon_capped_to_max_results() -> None:
    run = _run({"intent_text": "x"})
    statements = [f"pattern-{i}" for i in range(_KNOWLEDGE_MAX_RESULTS + 5)]
    framed = _assemble_executor_prompt(run, statements=statements)
    assert f"pattern-{_KNOWLEDGE_MAX_RESULTS - 1}" in framed
    # Beyond the cap is dropped.
    assert f"pattern-{_KNOWLEDGE_MAX_RESULTS}" not in framed


def test_per_statement_char_cap_applied() -> None:
    run = _run({"intent_text": "x"})
    long_statement = "z" * (_KNOWLEDGE_MAX_CHARS_PER_STATEMENT + 200)
    framed = _assemble_executor_prompt(run, statements=[long_statement])
    # The single statement is clamped — the full overlong string is not present.
    assert long_statement not in framed
    assert "z" * _KNOWLEDGE_MAX_CHARS_PER_STATEMENT in framed


def test_intent_capped_to_sensible_kb() -> None:
    run = _run({"intent_text": "y" * (_INTENT_MAX_CHARS + 1000)})
    framed = _assemble_executor_prompt(run, statements=[])
    # The intent is now the real instruction (cap lifted past 512) but still
    # bounded — respect the local-model generation budget.
    assert len(framed) <= _INTENT_MAX_CHARS + len(_EXECUTOR_SYSTEM_PROMPT) + 4096
    assert ("y" * _INTENT_MAX_CHARS) in framed


def test_intent_cap_is_larger_than_legacy_512() -> None:
    # B8 lifts the bare 512-char cap now that the prompt is the real instruction.
    assert _INTENT_MAX_CHARS > 512


def test_blank_statements_filtered() -> None:
    run = _run({"intent_text": "x"})
    framed = _assemble_executor_prompt(run, statements=["", "   ", "real pattern"])
    assert "real pattern" in framed
    # No stray bullet for the blank entries.
    assert framed.count("- ") == 1


def test_resolved_decisions_parses_list() -> None:
    run = _run(
        {"resolved_decisions": [{"question": "q", "answer": "a"}, "garbage", {"answer": ""}]}
    )
    decisions = _resolved_decisions(run)
    assert decisions == [("q", "a")]


def test_resolved_decisions_empty_when_missing() -> None:
    assert _resolved_decisions(_run({"intent_text": "x"})) == []
    assert _resolved_decisions(_run({"resolved_decisions": "not-a-list"})) == []
