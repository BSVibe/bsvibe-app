"""The round-cap-reached Decision asks WHAT to build, not WHY it failed.

Founder ruling 2026-09-08: the ``round_cap_reached`` Decision used to always ask the
founder to diagnose an internal fact ("is this possible as scoped, or does the
approach need to change?") using words (budget/rounds/approach) the founder never
chose. ``_drive_loop.py`` now offers a grounded DELIVERABLE choice instead, built by
``backend.workflow.domain.round_cap_outcome.round_cap_outcome_choice`` from the run's
own ``VerificationResult`` history:

* several DIFFERENT things failed across attempts → a "too big" framing (ship what's
  done, keep going on the rest, or split differently);
* the SAME thing failed every attempt → a "stuck on one part" framing (skip it, or
  keep going as is);
* ⭐ negative control 1 — the produced question/options never contain the banned
  internal vocabulary (budget / round / attempt / approach / verify / ceiling /
  reviewer, en+ko), through the SAME path the PWA reads (``_decision_options``);
* ⭐ negative control 2 — when nothing measurable supports a choice (no verification
  history and nothing written), the loop falls back to the existing
  ``verification_failed`` Decision and the run does not crash;
* ⭐ negative control 3 — the round cap's own PRE-cap stages (the stuck-review hint,
  the below-ceiling budget lift, ``request_review``) are covered by their own
  dedicated suites (``test_drive_loop_stuck_review_hint.py`` /
  ``tests/execution/test_request_review.py``) and are untouched by this change.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application._checkpoint_shared import _decision_options, _question_text
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

# The exact banned vocabulary a founder never chose (en/ko) — must never leak into
# any user-facing question/option text this Decision produces.
_BANNED_EN = ("budget", "round", "attempt", "approach", "verif", "ceiling", "reviewer")
_BANNED_KO = ("예산", "라운드", "시도", "접근", "검증", "천장", "검토자")


class ScriptedLlm:
    """Deterministic LLM stub — pops the next scripted turn."""

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


async def _make_run(
    session: AsyncSession, *, workspace_id: uuid.UUID | None = None
) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": "build the reporting dashboard"},
    )
    session.add(run)
    await session.flush()
    return run


async def _make_ko_workspace(session: AsyncSession) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    session.add(WorkspaceRow(id=workspace_id, name="ko workspace", language="ko"))
    await session.flush()
    return workspace_id


async def _only_decision(session: AsyncSession, run_id: uuid.UUID) -> Decision:
    rows = (await session.execute(select(Decision).where(Decision.run_id == run_id))).scalars()
    decisions = rows.all()
    assert len(decisions) == 1
    return decisions[0]


def _assert_no_banned_vocabulary(text: str, *, banned: tuple[str, ...]) -> None:
    lowered = text.lower()
    for word in banned:
        assert word not in lowered and word not in text, f"{word!r} leaked into: {text!r}"


# ── "too big": several DISTINCT failures across attempts → a split choice ───


async def test_distinct_failures_get_a_too_big_style_choice(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("false"), _tc("file_write", path="a.py", content="x")),
            ),
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #1 ("false")
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
            LoopTurn(content="", tool_calls=(_declare_command("nonexistent-cmd-xyz"),)),
            LoopTurn(content="still working", tool_calls=()),  # → verify FAILED #2 (different)
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
        ]
    )

    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=4
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "needs_decision"

        decision = await _only_decision(session, run.id)
        assert decision.decision == "ask_user_question"
        assert decision.payload.get("reason") == "round_cap_reached"

        question = _question_text(decision, "en")
        options = _decision_options(decision)  # the SAME path the PWA screen reads
        assert options is not None and len(options) >= 2
        assert "Finish just what's built so far" in options
        _assert_no_banned_vocabulary(question, banned=_BANNED_EN)
        for opt in options:
            _assert_no_banned_vocabulary(opt, banned=_BANNED_EN)


# ── "stuck on one part": the SAME failure every attempt → a skip-it choice ──


async def test_repeated_identical_failure_gets_a_stuck_style_choice(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("false"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #1
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #2, same contract
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #3, same contract
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=4
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "needs_decision"

        decision = await _only_decision(session, run.id)
        assert decision.decision == "ask_user_question"

        options = _decision_options(decision)
        assert options is not None
        assert "Skip that part and show me the rest first" in options


# ── ⭐ NC1: no banned vocabulary, in EITHER language ─────────────────────────


async def test_choice_has_no_banned_vocabulary_in_korean_workspace(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(_declare_command("false"), _tc("file_write", path="m", content="x")),
            ),
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #1
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
            LoopTurn(content="", tool_calls=()),  # → verify FAILED #2, same contract
            LoopTurn(content="", tool_calls=()),  # its automatic demo completion
        ]
    )
    async with memory_session() as session:
        workspace_id = await _make_ko_workspace(session)
        run = await _make_run(session, workspace_id=workspace_id)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=3
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "needs_decision"

        decision = await _only_decision(session, run.id)
        assert decision.decision == "ask_user_question"

        question = _question_text(decision, "ko")
        options = _decision_options(decision) or []
        assert options, "a ko workspace must still get options, not free-text-only"
        _assert_no_banned_vocabulary(question, banned=_BANNED_KO)
        for opt in options:
            _assert_no_banned_vocabulary(opt, banned=_BANNED_KO)


# ── ⭐ NC2: nothing measurable → falls back, run does not crash ─────────────


async def test_no_measurable_signal_falls_back_and_does_not_crash(tmp_path: Path) -> None:
    """The agent changes a file through the shell (never through ``file_write`` /
    ``file_edit``, so the server records zero writes) and never declares a way to
    prove it — the same shape as the ``fae09a47`` prod run in
    ``tests/execution/test_run_orchestrator.py``. No ``VerificationResult`` is ever
    created (``settle_undeclared_verification`` only creates one on a trivial NO-OP
    pass) and ``written_paths`` stays empty, so ``round_cap_outcome_choice`` has
    nothing measurable to build a menu from. The loop must keep today's plain
    ``verification_failed`` Decision instead of inventing one, and the run must reach
    a clean ``needs_decision`` terminal rather than raising."""
    import asyncio

    git = await asyncio.create_subprocess_exec("git", "init", "-q", cwd=tmp_path)
    assert await git.wait() == 0
    llm = ScriptedLlm(
        [
            LoopTurn(content="", tool_calls=(_tc("shell_exec", command="echo hi > slipped.txt"),)),
            *(LoopTurn(content="done", tool_calls=()) for _ in range(3)),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=4
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)  # must not raise

        assert result.outcome == "needs_decision"
        decision = await _only_decision(session, run.id)
        assert decision.decision == "verification_failed"
        assert decision.payload.get("reason") == "round_cap_reached"
        assert decision.payload.get("options") is None

        rows = (
            (await session.execute(select(Decision).where(Decision.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1, "the round cap is the terminal, not a founder park mid-loop"

        question = _question_text(decision, "en")
        assert question  # never blank
        _assert_no_banned_vocabulary(question, banned=_BANNED_EN)
