"""AgentRunner.drive — the transactional runner delegates the compute
loop to RunOrchestrator and reconciles the run status with the outcome."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.execution.db import ExecutionRun, ExecutionRunHistory, RunStatus
from backend.supervisor.sandbox import NoopSandboxManager, SandboxUnavailable
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from tests._support import memory_session


class _ScriptedLlm:
    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        return self._turns.pop(0)


class _FailingSandbox:
    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> Any:
        raise SandboxUnavailable("down")

    async def release(self, project_id: uuid.UUID) -> None:
        return None


async def _seed_run(session: Any) -> uuid.UUID:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"intent_text": "write the answer"},
    )
    session.add(run)
    await session.flush()
    return run.id


async def test_drive_verified_transitions_to_review_ready(tmp_path: Path) -> None:
    llm = _ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    LoopToolCall(
                        id="d1",
                        name="declare_verification",
                        arguments={"checks": [{"kind": "command", "command": "test -f out"}]},
                    ),
                    LoopToolCall(
                        id="w1", name="file_write", arguments={"path": "out", "content": "ok"}
                    ),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run_id = await _seed_run(session)
        runner = AgentRunner(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await runner.drive(run_id=run_id, orchestrator=orch, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY
        # History recorded open→running→review_ready.
        history = (
            (
                await session.execute(
                    select(ExecutionRunHistory).order_by(ExecutionRunHistory.created_at)
                )
            )
            .scalars()
            .all()
        )
        to_statuses = [h.to_status for h in history]
        assert RunStatus.RUNNING in to_statuses
        assert RunStatus.REVIEW_READY in to_statuses


async def test_drive_needs_decision_stays_running(tmp_path: Path) -> None:
    llm = _ScriptedLlm(
        [
            LoopTurn(
                content="blocked",
                tool_calls=(
                    LoopToolCall(
                        id="a1", name="ask_user_question", arguments={"question": "which env?"}
                    ),
                ),
            ),
        ]
    )
    async with memory_session() as session:
        run_id = await _seed_run(session)
        runner = AgentRunner(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await runner.drive(run_id=run_id, orchestrator=orch, workspace_dir=tmp_path)

        assert result.outcome == "needs_decision"
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.RUNNING  # paused, not terminal


async def test_drive_system_error_transitions_to_failed(tmp_path: Path) -> None:
    llm = _ScriptedLlm([LoopTurn(content="x", tool_calls=())])
    async with memory_session() as session:
        run_id = await _seed_run(session)
        runner = AgentRunner(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=_FailingSandbox())
        result = await runner.drive(run_id=run_id, orchestrator=orch, workspace_dir=tmp_path)

        assert result.outcome == "system_error"
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
