"""``request_review`` — a clean-context second opinion for a stuck agent.

prod measured a stuck agent repeat the SAME failing approach instead of changing it
(runs 010bbdd8, 40c14a10, 0093fce6 — see ``backend/workflow/domain/request_review.py``).
These tests pin the tool's four contractual properties:

* it gathers the run's OWN full attempt history (every ``VerificationResult``, not just
  the last) and hands it to a review turn that runs through the loop's OWN ``LoopLlm``;
* ⭐ negative control 1 — refused with no attempt history at all;
* ⭐ negative control 2 — refused once the per-run call budget is spent, and the refusal
  is a message the CALLER reads (not a silent no-op);
* ⭐ negative control 3 — the existing ``declare_verification`` verify-first gate is
  completely unaffected by this tool's existence.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.domain.request_review import MAX_STUCK_REVIEWS_PER_RUN, REQUEST_REVIEW_NAME
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus, VerificationResult
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class ScriptedLlm:
    """Deterministic LLM stub — pops the next scripted turn.

    Serves BOTH the drive loop's own plan turns AND the reviewer's completion (it is the
    SAME ``LoopLlm`` — ``orch._llm`` — the review handler reuses per the v1 design), so a
    scripted "reviewer" turn interleaves in the queue exactly where ``handle_request_review``
    calls ``llm.complete`` mid-turn.
    """

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _declare_command(command: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "command", "command": command}])


async def _make_run(session: AsyncSession, *, intent: str = "do the thing") -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    session.add(run)
    await session.flush()
    return run


async def _verification_rows(session: AsyncSession, run_id: uuid.UUID) -> list[VerificationResult]:
    stmt = (
        select(VerificationResult)
        .where(VerificationResult.run_id == run_id)
        .order_by(VerificationResult.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _tool_messages(calls: list[dict[str, Any]]) -> list[str]:
    """Every ``role == "tool"`` message content across every scripted call — the plain
    text the agent actually reads back, whichever turn it landed on."""
    out: list[str] = []
    for call in calls:
        for message in call["messages"]:
            if message.get("role") == "tool":
                out.append(str(message.get("content") or ""))
    return out


# ── the tool is on the menu ──────────────────────────────────────────────


async def test_request_review_tool_advertised_in_loop_schema(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("true"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)
    tools = llm.calls[0]["tools"]
    names = {(t.get("function") or {}).get("name") for t in tools}
    assert REQUEST_REVIEW_NAME in names


# ── negative control 1: no attempt history at all → refused ─────────────


async def test_refused_with_no_verification_history(tmp_path: Path) -> None:
    """Calling it before ANY verification attempt gets a readable refusal, and no LLM
    completion is spent on a reviewer that would have nothing to read."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="let me get a second opinion first",
                tool_calls=(_tc(REQUEST_REVIEW_NAME),),
            ),
            LoopTurn(
                content="",
                tool_calls=(_declare_command("true"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="done", tool_calls=()),
            # Verify PASSES on the first real attempt, so exactly one automatic
            # outcome-demonstration completion follows (see ``verification_service``'s
            # ``_run_outcome_demonstration`` — it runs on every verify with written
            # paths, independent of this tool). A harmless non-JSON reply degrades it
            # to "undemonstrable" rather than failing the run.
            LoopTurn(content="", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

    # No history → refused WITHOUT spending a reviewer completion: exactly the 4
    # scripted completions were consumed (3 agent turns + 1 automatic demo judge),
    # nothing extra for the refused review.
    assert len(llm.calls) == 4
    tool_texts = _tool_messages(llm.calls)
    assert any("has not attempted verification yet" in t for t in tool_texts)
    assert any("refused" in t for t in tool_texts)


# ── the primary contract: FULL history, not just the latest failure ─────


async def test_reviewer_sees_every_attempt_not_just_the_last(tmp_path: Path) -> None:
    """Two FAILED attempts happen before the agent asks for review. The reviewer's OWN
    completion call must show BOTH — the whole point of this tool over the loop's
    existing "Fix the problem and try again" message, which only ever repeats the
    latest failure back to the same agent that just produced it."""
    llm = ScriptedLlm(
        [
            # turn0 — declares a check that will FAIL, and writes the artifact.
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("false"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            # turn1 — no tool calls → the loop verifies → FAILED attempt #1.
            LoopTurn(content="", tool_calls=()),
            # automatic outcome-demonstration completion for that verify (harmless).
            LoopTurn(content="", tool_calls=()),
            # turn2 — no tool calls again → verifies the SAME failing contract → FAILED #2.
            LoopTurn(content="", tool_calls=()),
            LoopTurn(content="", tool_calls=()),  # its demo completion
            # turn3 — finally asks for a second opinion.
            LoopTurn(
                content="I'm stuck",
                tool_calls=(_tc(REQUEST_REVIEW_NAME, context="two failures so far"),),
            ),
            # the REVIEWER's own completion, popped by handle_request_review mid-turn.
            LoopTurn(
                content="You keep declaring `false` itself — declare a real check.", tool_calls=()
            ),
            # turn4 — takes the advice: declares a passing check.
            LoopTurn(content="", tool_calls=(_declare_command("test -f marker"),)),
            # turn5 — no tool calls → verifies → PASSED.
            LoopTurn(content="done", tool_calls=()),
            LoopTurn(content="", tool_calls=()),  # its demo completion
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        rows = await _verification_rows(session, run.id)
    assert [r.outcome.value for r in rows] == ["failed", "failed", "passed"]

    # The reviewer's own call is index 6: turn0, turn1, demo, turn2, demo, turn3,
    # THEN the reviewer.
    reviewer_call = llm.calls[6]
    user_messages = [m for m in reviewer_call["messages"] if m.get("role") == "user"]
    assert len(user_messages) == 1
    history_text = str(user_messages[0]["content"])
    # Both attempts are visible, at the time only 2 had happened — NOT just the last.
    assert "Attempt 1/2" in history_text
    assert "Attempt 2/2" in history_text
    assert "two failures so far" in history_text  # the agent's own extra context, too
    system_messages = [m for m in reviewer_call["messages"] if m.get("role") == "system"]
    assert any("stuck" in str(m["content"]).lower() for m in system_messages)

    # The reviewer's feedback is what the agent reads on its very next turn.
    tool_texts = _tool_messages(llm.calls)
    assert any("declare a real check" in t for t in tool_texts)


# ── negative control 2: the per-run call budget caps, and refusal is readable ──


async def test_call_budget_is_capped_and_the_refusal_is_readable(tmp_path: Path) -> None:
    turns: list[LoopTurn] = [
        # turn0 — declares a failing check + writes, so there is history to review.
        LoopTurn(
            content="",
            tool_calls=(_declare_command("false"), _tc("file_write", path="marker", content="x")),
        ),
        # turn1 — no tool calls → verifies → FAILED #1.
        LoopTurn(content="", tool_calls=()),
        LoopTurn(content="", tool_calls=()),  # its demo completion
    ]
    # MAX_STUCK_REVIEWS_PER_RUN accepted review round-trips (agent call + reviewer reply).
    for i in range(MAX_STUCK_REVIEWS_PER_RUN):
        turns.append(LoopTurn(content=f"review #{i + 1}", tool_calls=(_tc(REQUEST_REVIEW_NAME),)))
        turns.append(LoopTurn(content=f"reviewer feedback #{i + 1}", tool_calls=()))
    # One more — over budget. No reviewer turn follows: it must be refused before any
    # extra completion is spent.
    turns.append(LoopTurn(content="one more please", tool_calls=(_tc(REQUEST_REVIEW_NAME),)))
    # takes the advice, declares a passing check, then verifies PASSED (+ its demo).
    turns.append(LoopTurn(content="", tool_calls=(_declare_command("test -f marker"),)))
    turns.append(LoopTurn(content="done", tool_calls=()))
    turns.append(LoopTurn(content="", tool_calls=()))  # its demo completion

    llm = ScriptedLlm(turns)
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

    # Exactly ``len(turns)`` completions total: the over-budget call did NOT consume an
    # extra (unscripted) reviewer completion — ScriptedLlm would have raised otherwise.
    assert len(llm.calls) == len(turns)
    tool_texts = _tool_messages(llm.calls)
    assert any(
        "refused" in t and str(MAX_STUCK_REVIEWS_PER_RUN) in t and "review call" in t
        for t in tool_texts
    ), tool_texts


# ── negative control 3: the existing verify-first gate is unaffected ─────


async def test_declare_verification_gate_is_unaffected_by_request_review(tmp_path: Path) -> None:
    """request_review is a loop-owned pseudo-tool, entirely outside ``ToolRegistry`` — the
    write-gate ``declare_verification`` guards must behave exactly as before its addition."""
    from backend.workflow.infrastructure.tools import ToolError, ToolRegistry

    registry = ToolRegistry(workspace_dir=tmp_path)
    with pytest.raises(ToolError, match="declare_verification"):
        await registry.invoke("file_write", {"path": "out.txt", "content": "hi"})
    assert not (tmp_path / "out.txt").exists()

    await registry.invoke(
        "declare_verification", {"checks": [{"kind": "command", "command": "true"}]}
    )
    result = await registry.invoke("file_write", {"path": "out.txt", "content": "hi"})
    assert "wrote" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"
