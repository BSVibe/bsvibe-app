"""RunOrchestrator compute-loop tests (execution layer, no HTTP).

A deterministic stub LLM drives the loop; a real host-side
``NoopSandboxManager`` does the file work + runs the verify command
checks. These prove the §11.3 plan → act → verify → iterate loop end to
end without touching a real model or Docker.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.delivery.db import DeliveryEventRow
from backend.execution.db import (
    Decision,
    Deliverable,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import (
    CanonRetriever,
    LoopLlm,
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.supervisor.sandbox import NoopSandboxManager, SandboxUnavailable
from tests._support import memory_session

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class ScriptedLlm:
    """A deterministic :class:`LoopLlm` — pops the next pre-programmed
    turn on each ``complete`` call (FIFO). Records the (messages, tools)
    each call saw for assertions. Raises if the loop asks for more turns
    than scripted (catches runaway loops)."""

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


class FailingSandboxManager:
    """A sandbox manager whose acquire blows up — simulates infra failure."""

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> Any:
        raise SandboxUnavailable("docker daemon unreachable")

    async def release(self, project_id: uuid.UUID) -> None:
        return None


class StubRetriever:
    """A :class:`CanonRetriever` returning fixed canonical patterns."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _declare_command(command: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "command", "command": command}])


def _declare_judge(*criteria: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "judge", "criteria": list(criteria)}])


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


# --------------------------------------------------------------------------
# verified path — real file work + a command check that passes
# --------------------------------------------------------------------------


async def test_verified_run_does_file_work_and_passes_command_check(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I'll write the answer and declare a check.",
                tool_calls=(
                    _declare_command("grep -q 42 answer.txt"),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done — the answer is written.", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert result.written_paths == ["answer.txt"]
        # Real file landed on disk.
        assert (tmp_path / "answer.txt").read_text() == "42\n"

        work_step = (await session.execute(select(WorkStep))).scalar_one()
        assert work_step.status is WorkStepStatus.VERIFIED
        assert work_step.proof_state is ProofState.PROVED

        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.COMPLETED

        vr = (await session.execute(select(VerificationResult))).scalar_one()
        assert vr.outcome is VerificationOutcome.PASSED

        deliverable = (await session.execute(select(Deliverable))).scalar_one()
        assert "answer.txt" in (deliverable.payload.get("artifact_refs") or [])

        # A Deliver event was emitted into the table the DeliveryWorker drains.
        deliver_event = (await session.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable.id

        # Settle observation recorded as run activity.
        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert any(a.activity_type == "settle" for a in activities)


async def test_verified_run_no_extra_llm_calls(tmp_path: Path) -> None:
    """A command-only contract needs no judge call — the loop must not
    make an unscripted LLM round."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
    assert result.outcome == "verified"
    assert len(llm.calls) == 2  # two plan turns, zero judge calls


# --------------------------------------------------------------------------
# judge path — non-executable criteria graded by the LLM
# --------------------------------------------------------------------------


async def test_verified_via_judge_check(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_judge("the file greets the world"),
                    _tc("file_write", path="hello.txt", content="Hello, world"),
                ),
            ),
            LoopTurn(content="written the greeting", tool_calls=()),
            # judge call (tools=None) returns a pass verdict as JSON
            LoopTurn(content='{"passed": true, "reasoning": "greets the world"}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

    assert result.outcome == "verified"
    # The judge call was made with no tools.
    assert llm.calls[-1]["tools"] is None


async def test_judge_fail_then_decision_at_cap(tmp_path: Path) -> None:
    """A judge verdict of fail with the cycle cap reached → needs_decision."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_judge("the file contains a valid proof"),
                    _tc("file_write", path="proof.txt", content="nope"),
                ),
            ),
            LoopTurn(content="attempted", tool_calls=()),
            LoopTurn(content='{"passed": false, "reasoning": "no proof present"}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=2
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

    assert result.outcome == "needs_decision"
    assert result.decision_id is not None


# --------------------------------------------------------------------------
# iterate: verify fails, re-plan, then pass
# --------------------------------------------------------------------------


async def test_failed_verify_then_replan_then_verified(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("grep -q DONE result.txt"),
                    _tc("file_write", path="result.txt", content="WIP"),
                ),
            ),
            LoopTurn(content="first pass", tool_calls=()),  # → verify FAIL
            LoopTurn(
                content="fixing", tool_calls=(_tc("file_write", path="result.txt", content="DONE"),)
            ),
            LoopTurn(content="second pass", tool_calls=()),  # → verify PASS
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager(), max_cycles=6
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        results = (await session.execute(select(VerificationResult))).scalars().all()
        outcomes = sorted(r.outcome.value for r in results)
        assert "failed" in outcomes
        assert "passed" in outcomes


# --------------------------------------------------------------------------
# needs_decision paths
# --------------------------------------------------------------------------


async def test_ask_user_question_creates_decision_and_pauses(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="I need the founder to decide.",
                tool_calls=(_tc("ask_user_question", question="Which database should I target?"),),
            ),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert result.decision_id == decision.id
        assert "database" in (decision.payload.get("question") or "")
        # No verification ran.
        assert (await session.execute(select(VerificationResult))).first() is None
        # The work step did NOT reach a verified terminal.
        work_step = (await session.execute(select(WorkStep))).scalar_one()
        assert work_step.status is not WorkStepStatus.VERIFIED


async def test_no_contract_declared_routes_to_decision(tmp_path: Path) -> None:
    """Work that finishes without ever declaring a contract is never a
    silent pass — it becomes a human-review Decision."""
    llm = ScriptedLlm(
        [
            LoopTurn(content="", tool_calls=(_tc("file_write", path="foo.txt", content="bar"),)),
            LoopTurn(content="I think I'm done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        decision = (await session.execute(select(Decision))).scalar_one()
        assert decision.decision == "human_review_required"


# --------------------------------------------------------------------------
# system_error path
# --------------------------------------------------------------------------


class _RaisingLlm:
    """A LoopLlm that blows up mid-plan — simulates an in-loop crash."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        raise RuntimeError("model backend exploded")


async def test_loop_crash_yields_system_error(tmp_path: Path) -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session, llm=_RaisingLlm(), sandbox_manager=NoopSandboxManager()
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "system_error"
        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.FAILED
        # The crash was recorded as an activity, not leaked.
        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert any(a.activity_type == "error" for a in activities)


async def test_sandbox_failure_yields_system_error(tmp_path: Path) -> None:
    llm = ScriptedLlm([LoopTurn(content="never reached", tool_calls=())])
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=FailingSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "system_error"
        attempt = (await session.execute(select(RunAttempt))).scalar_one()
        assert attempt.phase is RunAttemptPhase.FAILED


# --------------------------------------------------------------------------
# BSage retrieval folds canonical patterns into the contract as judge criteria
# --------------------------------------------------------------------------


async def test_retriever_folds_canonical_patterns_into_contract(tmp_path: Path) -> None:
    retriever = StubRetriever(["always pin dependency versions"])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f deps.txt"),
                    _tc("file_write", path="deps.txt", content="pkg==1.0"),
                ),
            ),
            LoopTurn(content="declared deps", tool_calls=()),
            # judge call for the folded canonical criterion
            LoopTurn(content='{"passed": true}', tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            retriever=retriever,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert retriever.queried, "retriever should have been queried with the change signals"
        vr = (await session.execute(select(VerificationResult))).scalar_one()
        # The folded canonical pattern shows up as a judge criterion in the contract.
        contract_blob = str(vr.contract)
        assert "pin dependency versions" in contract_blob


def test_loop_protocols_are_runtime_checkable() -> None:
    assert isinstance(ScriptedLlm([]), LoopLlm)
    assert isinstance(StubRetriever([]), CanonRetriever)
