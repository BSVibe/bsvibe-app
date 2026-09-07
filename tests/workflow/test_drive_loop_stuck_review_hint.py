"""The drive loop's OWN "Verification FAILED" feedback, not the ``request_review`` tool
menu entry — see ``backend/workflow/domain/request_review.py``'s module docstring, which
names this exact message (``"Fix the problem and try again"``) as the gap a stuck agent
never reads a pointer to ``request_review`` in.

These tests pin the feedback the agent actually reads on its NEXT turn:

* on the FIRST failure the default sentence is unchanged — no hint (a first failure is
  usually a typo, and retry is the right move);
* on the SECOND (and later) consecutive failure, the hint IS appended, pointing at
  ``request_review``, while the default sentence still leads;
* ⭐ negative control — once the ``request_review`` call budget is spent, the hint
  disappears even on a repeated failure, so the agent is never told to call a tool that
  will only refuse it.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.domain.request_review import (
    MAX_STUCK_REVIEWS_PER_RUN,
    REQUEST_REVIEW_NAME,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

_DEFAULT_SENTENCE = "Fix the problem and try again, then send your summary."
_HINT_MARKER = REQUEST_REVIEW_NAME


class ScriptedLlm:
    """Deterministic LLM stub — pops the next scripted turn (mirrors
    ``tests/execution/test_request_review.py``'s ``ScriptedLlm``)."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages)})
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _declare_command(command: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "command", "command": command}])


async def _make_run(session: AsyncSession) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": "do the thing"},
    )
    session.add(run)
    await session.flush()
    return run


def _failure_texts(calls: list[dict[str, Any]]) -> list[str]:
    """Every "Verification FAILED" user-turn body in the drive loop's OWN accumulated
    message history, in append order — the plain text the agent actually reads back.

    ``ScriptedLlm`` also serves the automatic outcome-demonstration judge, which is a
    SEPARATE short prompt built by ``verification_service`` — not the drive loop's
    growing ``messages`` list. The loop's own history is always the LONGEST message
    list among every scripted call, so picking the max avoids reading a demo call's
    unrelated prompt by accident (and avoids de-duplicating across calls, which would
    collapse two byte-identical failures — same contract, no hint — into one).
    """
    longest = max(calls, key=lambda c: len(c["messages"]))["messages"]
    return [
        str(m.get("content") or "")
        for m in longest
        if m.get("role") == "user" and str(m.get("content") or "").startswith("Verification FAILED")
    ]


# ── first failure: no hint, default sentence intact ──────────────────────────


async def test_first_failure_has_no_hint(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("false"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #1
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
            LoopTurn(content="", tool_calls=(_declare_command("true"),)),  # fixes it
            LoopTurn(content="done", tool_calls=()),  # → verify PASSED
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

    texts = _failure_texts(llm.calls)
    assert len(texts) == 1
    assert _DEFAULT_SENTENCE in texts[0]
    assert _HINT_MARKER not in texts[0]


# ── second+ consecutive failure: hint appears, default sentence still leads ──


async def test_second_consecutive_failure_points_at_request_review(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("false"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #1 (no hint)
            LoopTurn(content="", tool_calls=()),  # its demo completion
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #2, same contract (hint)
            LoopTurn(content="", tool_calls=()),  # its demo completion
            LoopTurn(content="", tool_calls=(_declare_command("true"),)),  # fixes it
            LoopTurn(content="done", tool_calls=()),  # → verify PASSED
            LoopTurn(content="", tool_calls=()),  # its demo completion
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

    texts = _failure_texts(llm.calls)
    assert len(texts) == 2
    # Failure #1: default sentence only.
    assert _DEFAULT_SENTENCE in texts[0]
    assert _HINT_MARKER not in texts[0]
    # Failure #2: default sentence STILL there (appended to, not replaced) + the hint.
    assert _DEFAULT_SENTENCE in texts[1]
    assert _HINT_MARKER in texts[1]


# ── negative control: budget already spent → hint suppressed even on repeat ──


async def test_hint_suppressed_once_review_budget_is_spent(tmp_path: Path) -> None:
    assert MAX_STUCK_REVIEWS_PER_RUN == 2, "test wiring assumes exactly 2 review calls fit"
    turns = [
        LoopTurn(
            content="",
            tool_calls=(_declare_command("false"), _tc("file_write", path="m", content="x")),
        ),
        LoopTurn(content="", tool_calls=()),  # → verify FAILED #1
        LoopTurn(content="", tool_calls=()),  # its demo completion
    ]
    # Spend the entire request_review budget across two cycles that do NOT verify.
    for i in range(MAX_STUCK_REVIEWS_PER_RUN):
        turns.append(LoopTurn(content=f"review #{i + 1}", tool_calls=(_tc(REQUEST_REVIEW_NAME),)))
        turns.append(LoopTurn(content=f"reviewer feedback #{i + 1}", tool_calls=()))
    turns += [
        LoopTurn(content="", tool_calls=()),  # → verify FAILED #2, same contract, budget spent
        LoopTurn(content="", tool_calls=()),  # its demo completion
        LoopTurn(content="", tool_calls=(_declare_command("true"),)),  # fixes it
        LoopTurn(content="done", tool_calls=()),  # → verify PASSED
        LoopTurn(content="", tool_calls=()),  # its demo completion
    ]
    llm = ScriptedLlm(turns)
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

    texts = _failure_texts(llm.calls)
    assert len(texts) == 2
    assert _DEFAULT_SENTENCE in texts[0]
    assert _HINT_MARKER not in texts[0]
    # Failure #2 is the SECOND consecutive failure, but the budget is exhausted — the
    # hint must not tell the agent to call a tool that can only refuse it.
    assert _DEFAULT_SENTENCE in texts[1]
    assert _HINT_MARKER not in texts[1]
