"""The sandbox venv is provisioned ONCE at acquire — so the inline
``shell_exec`` path (not just the verify stage) sees a ready ``.venv``.

RunOrchestrator.run must call ``ensure_sandbox_ready(box)`` exactly once,
right after a successful acquire and before the drive loop; and a failed /
raising provision must be logged and swallowed (best-effort) so it never
becomes a ``system_error`` or crashes the run.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

import backend.workflow.application.agent_loop as agent_loop_mod
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session


class _ScriptedLlm:
    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        return self._turns.pop(0)


def _verified_llm() -> _ScriptedLlm:
    """A turn that declares a command check + writes the file it checks, then a
    closing turn — drives the loop to a real ``verified`` terminal outcome (the
    NoopSandboxManager runs ``test -f out`` against the written file)."""
    return _ScriptedLlm(
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


async def test_acquire_provisions_venv_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_sandbox_ready`` is invoked exactly once, with the acquired box,
    after acquire — before the drive loop runs any shell_exec."""
    seen: list[Any] = []

    async def _fake_ready(box: Any) -> bool:
        seen.append(box)
        return True

    monkeypatch.setattr(agent_loop_mod, "ensure_sandbox_ready", _fake_ready)

    async with memory_session() as session:
        run_id = await _seed_run(session)
        runner = AgentRunner(session)
        orch = RunOrchestrator(
            session=session, llm=_verified_llm(), sandbox_manager=NoopSandboxManager()
        )
        result = await runner.drive(run_id=run_id, orchestrator=orch, workspace_dir=tmp_path)

    assert result.outcome == "verified"
    assert len(seen) == 1  # provisioned once, at acquire
    assert hasattr(seen[0], "workspace_mount")  # the acquired sandbox session


async def test_acquire_provision_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provisioning that RAISES is logged and swallowed — the run proceeds
    through the loop to its normal outcome, never a system_error at acquire."""

    async def _boom(box: Any) -> bool:
        raise RuntimeError("uv sync exploded")

    monkeypatch.setattr(agent_loop_mod, "ensure_sandbox_ready", _boom)

    async with memory_session() as session:
        run_id = await _seed_run(session)
        runner = AgentRunner(session)
        orch = RunOrchestrator(
            session=session, llm=_verified_llm(), sandbox_manager=NoopSandboxManager()
        )
        result = await runner.drive(run_id=run_id, orchestrator=orch, workspace_dir=tmp_path)

    # The failed provision did NOT crash the drive nor short-circuit to
    # system_error — the loop ran and verified normally.
    assert result.outcome == "verified"
