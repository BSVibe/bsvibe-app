"""[P] INV-3 production proof — a due schedule becomes a real Run (S1).

The anti-unwired-stub gate for the Schedule input track. It drives the WHOLE
``workspace_schedules`` channel through the REAL authoring surface and the REAL
worker chain, with NO ``WorkspaceScheduleRow`` seeding and NO API
``dependency_overrides`` (a real HS256 JWT verified by the production auth path,
against the CI Postgres):

    POST /api/v1/schedules            → WorkspaceScheduleRow (via the producer emit)
      → ScheduleWorker.fire_due_once  → TriggerEventRow (payload carries ``text``)
      → IntakeWorker.drain_once       → RequestRow (OPEN, payload carries ``text``)
      → AgentWorker.claim_once        → ExecutionRun (OPEN)
      → AgentWorker.drive_once        → frame → run.payload["intent_text"]

The assertion is the crux of the slice: the framed intent text equals the
SUBMITTED instruction — NOT "Untitled run". Before S1 this test could not be
written (no authoring surface to POST to, and even a hand-seeded row framed as
"Untitled run" because the emitter never carried an instruction). That
impossibility WAS the bug.

Only the worker-side deps (a stub frame LLM + a scripted work LLM on a host-side
NoopSandbox) are test doubles — those are worker construction args, NOT API
dependency overrides. The authoring path (POST → ScheduleService → producer emit)
and the RLS/auth request graph run entirely for real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.schedule.domain.advancer import CronScheduleAdvancer
from backend.schedule.infrastructure.db_poll_runner import DbPollScheduleRunner
from backend.schedule.infrastructure.workers.schedule_worker import ScheduleWorker
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .._support import BuildFrameLlm
from .conftest import bootstrap_tenant, client_for, mint_jwt, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_INSTRUCTION = "post the weekly market summary to the team channel"


class _ScriptedLlm:
    """Deterministic work LLM — writes an artifact + declares a check, verifies."""

    def __init__(self) -> None:
        self._turns = [
            LoopTurn(
                content="Writing the deliverable.",
                tool_calls=(
                    LoopToolCall(
                        id="c1",
                        name="declare_verification",
                        arguments={"checks": [{"kind": "command", "command": "test -f out.txt"}]},
                    ),
                    LoopToolCall(
                        id="c2", name="file_write", arguments={"path": "out.txt", "content": "ok\n"}
                    ),
                ),
            ),
            LoopTurn(content="Done.", tool_calls=()),
        ]

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        return self._turns.pop(0)


def _execution_deps(workspace_root: Path) -> AgentExecutionDeps:
    from backend.extensions.skill.loader import SkillLoader

    llm = _ScriptedLlm()

    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(workspace_root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=lambda session, _run: RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager()
        ),
        workspace_root=workspace_root,
        frame_llm=BuildFrameLlm(),
    )


async def test_due_schedule_becomes_real_run_with_submitted_instruction(
    real_app: object,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    user_id = f"sched-user-{uuid.uuid4()}"
    workspace_id = await bootstrap_tenant(
        session_factory, supabase_user_id=user_id, email="sched@example.com"
    )
    token = mint_jwt(user_id, email="sched@example.com")

    # 1. Author the schedule through the REAL REST surface (no row seeding).
    async with client_for(real_app, token) as client:
        resp = await client.post(
            "/api/v1/schedules",
            json={
                "kind": "instruction",
                "text": _INSTRUCTION,
                "cron_expr": "* * * * *",
            },
        )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    next_run_at = datetime.fromisoformat(created["next_run_at"])

    # 2. Tick the ScheduleWorker with the clock just past the row's window so it
    #    is due — the emitter mints a TriggerEvent carrying the instruction.
    schedule_worker = ScheduleWorker(
        session_factory=session_factory,
        runner=DbPollScheduleRunner(
            advancer=CronScheduleAdvancer(),
            now_fn=lambda: next_run_at + timedelta(seconds=1),
        ),
    )
    assert await schedule_worker.fire_due_once() == 1

    # 3. IntakeWorker drains the TriggerEvent → Request (payload carries text).
    assert await IntakeWorker(session_factory=session_factory).drain_once() == 1

    # 4. AgentWorker claims the Request → ExecutionRun, then frames + drives it.
    agent = AgentWorker(session_factory=session_factory, execution=_execution_deps(tmp_path))
    assert await agent.claim_once() == 1
    assert await agent.drive_once() == 1

    # 5. THE PROOF — a real ExecutionRun exists whose framed intent text is the
    #    SUBMITTED instruction, not "Untitled run".
    async with session_factory() as session:
        run = (
            await session.execute(
                select(ExecutionRun).where(ExecutionRun.workspace_id == workspace_id)
            )
        ).scalar_one()
        assert run.payload.get("intent_text") == _INSTRUCTION
        assert run.payload.get("intent_text") != "Untitled run"
