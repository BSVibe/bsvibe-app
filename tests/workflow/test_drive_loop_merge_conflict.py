"""PR7 — the drive loop surfaces a re-dispatched merge conflict to the agent.

When the ``github_merge_watch`` worker finds a genuine conflict it writes
``run.payload["merge_conflict"]`` and re-opens the run. The drive loop turns
that into a turn-context instruction (paths + base branch) telling the agent to
resolve mechanical conflicts directly but raise a founder Decision for ambiguous
ones — then consumes the one-shot key (so a later resume does not re-inject it)
while leaving the ``merge_conflict_resolving`` marker that classifies a question
raised in this window as a ``merge_conflict_review`` Decision.

Pure helpers, tested directly (mirrors ``test_loop_sees_remote_tool_work.py``) —
the full drive_loop needs a whole RunOrchestrator, so the extracted helpers carry
the behaviour.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application._drive_loop import (
    _consume_merge_conflict,
    _merge_conflict_directive,
)


class _Run:
    """A minimal stand-in for ExecutionRun carrying just ``payload``."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload


def test_directive_carries_paths_and_base_branch() -> None:
    run = _Run(
        {
            "merge_conflict": {
                "conflict_paths": ["src/a.py", "src/b.py"],
                "base_branch": "develop",
                "pr_number": 7,
            }
        }
    )
    msg = _merge_conflict_directive(run)  # type: ignore[arg-type]
    assert msg is not None
    assert msg["role"] == "user"
    content = msg["content"]
    assert "develop" in content
    assert "src/a.py" in content and "src/b.py" in content
    # The clear-vs-ambiguous policy is spelled out for the agent.
    assert "MECHANICAL" in content
    assert "AMBIGUOUS" in content
    assert "ask_user_question" in content


def test_directive_is_none_without_conflict() -> None:
    assert _merge_conflict_directive(_Run(None)) is None  # type: ignore[arg-type]
    assert _merge_conflict_directive(_Run({})) is None  # type: ignore[arg-type]
    # A non-dict merge_conflict degrades to None (never raises).
    assert _merge_conflict_directive(_Run({"merge_conflict": "oops"})) is None  # type: ignore[arg-type]


def test_directive_tolerates_missing_paths() -> None:
    run = _Run({"merge_conflict": {"base_branch": "main"}})
    msg = _merge_conflict_directive(run)  # type: ignore[arg-type]
    assert msg is not None
    assert "main" in msg["content"]


def test_consume_clears_key_and_sets_resolving_marker() -> None:
    run = _Run(
        {
            "merge_conflict": {"conflict_paths": ["x.py"], "base_branch": "main"},
            "intent_text": "keep me",
        }
    )
    _consume_merge_conflict(run)  # type: ignore[arg-type]
    # The one-shot key is gone (so a later resume won't re-inject it) ...
    assert "merge_conflict" not in run.payload
    # ... a persistent marker remains so the ask path classifies the kind ...
    assert run.payload["merge_conflict_resolving"] is True
    # ... and unrelated payload survives.
    assert run.payload["intent_text"] == "keep me"


def test_consume_is_noop_without_conflict() -> None:
    run = _Run({"intent_text": "keep me"})
    _consume_merge_conflict(run)  # type: ignore[arg-type]
    assert run.payload == {"intent_text": "keep me"}
    assert "merge_conflict_resolving" not in run.payload
