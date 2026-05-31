"""B9a — the Frame stage output is now CONSUMED by the agent loop.

Before B9a, ``AgentWorker._frame_and_drive`` wrote ``run.payload["frame"]`` and
nothing read it — skill matching + artifact-type hints were dead. These tests
prove the delta: a run whose frame matched a skill seeds that skill as a
first-invocation hint into the loop's initial context, and the richer framing
(framed_intent + path_classification) is persisted on the run payload.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _RecordingLlm:
    """A deterministic ``LoopLlm`` that records every (messages, tools) it saw
    and ends the loop immediately with prose (no work) — enough to inspect the
    seeded first-turn context."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        return LoopTurn(content="nothing to do", tool_calls=())


class _StubFrameLlm:
    """A ``FrameLlm`` returning canned structured framing."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload)

    async def complete_text(self, *, system: str, user: str) -> str:
        return self._payload


def _write_skill(root: Path, name: str, description: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n---\nbody",
        encoding="utf-8",
    )


async def _seed_request_and_run(
    session: AsyncSession, *, workspace_id: uuid.UUID, text: str
) -> uuid.UUID:
    # Seed the FK parent (TriggerEvent) BEFORE the Request — real Postgres
    # enforces requests_trigger_event_id_fkey (local SQLite does not).
    trigger = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source="direct",
        trigger_kind=TriggerKind.DIRECT,
        idempotency_key=f"k-{uuid.uuid4()}",
        payload={"text": text},
        received_at=datetime.now(tz=UTC),
    )
    session.add(trigger)
    await session.flush()
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trigger_event_id=trigger.id,
        status=RequestStatus.RUNNING,
        payload={"text": text},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(request)
    await session.flush()
    return await AgentRunner(session).open_run(request=request)


async def test_frame_skill_hint_reaches_loop_initial_context(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A run whose frame matched a skill → the loop's first-turn context carries
    the suggested-skill hint (the frame output is now READ, not ignored)."""
    workspace_id = uuid.uuid4()
    skills_root = tmp_path / "skills" / str(workspace_id)
    _write_skill(skills_root, "prd-writer", "Draft a product requirements document")

    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="i need a spec doc"
        )
        await session.commit()

    recording_llm = _RecordingLlm()

    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(tmp_path / "skills" / str(ws_id))
        loader.load_all()
        return loader

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        # The factory READS the frame the worker just recorded onto run.payload
        # and threads the matched skill (with its description) into the loop.
        frame = (run.payload or {}).get("frame") or {}
        skill_name = frame.get("skill_match")
        loader = _skill_loader_for(run.workspace_id)
        description = None
        if skill_name and skill_name in loader.registry:
            description = loader.registry[skill_name].description
        return RunOrchestrator(
            session=session,
            llm=recording_llm,
            sandbox_manager=NoopSandboxManager(),
            skill_loader=loader,
            suggested_skill=skill_name,
            suggested_skill_description=description,
        )

    deps = AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_orchestrator_factory,
        workspace_root=tmp_path / "runs",
        frame_llm=_StubFrameLlm(
            {
                "framed_intent": "Write a PRD",
                "skill_match": "prd-writer",
                "artifact_type_hint": "page",
                "path_classification": "agent_loop",
            }
        ),
    )
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    # The recorded frame is the richer LLM framing, persisted for B9b + delivery.
    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        frame = run.payload["frame"]
        assert frame["skill_match"] == "prd-writer"
        assert frame["artifact_type_hint"] == "page"
        assert frame["framed_intent"] == "Write a PRD"
        assert frame["path_classification"] == "agent_loop"

    # DELTA: the matched skill hint reached the loop's first-turn messages.
    blob = "\n".join(
        m.get("content", "")
        for m in recording_llm.calls[0]["messages"]
        if isinstance(m.get("content"), str)
    )
    assert "prd-writer" in blob
    assert "Draft a product requirements document" in blob


async def test_no_frame_llm_records_keyword_frame_and_no_skill_hint(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """No FrameLlm → keyword frame (no match here) recorded, no skill hint in the
    loop (regression: the no-LLM keyword path is unchanged)."""
    workspace_id = uuid.uuid4()
    _write_skill(
        tmp_path / "skills" / str(workspace_id),
        "weekly-digest",
        "Generate a weekly digest",
    )

    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="buy groceries"
        )
        await session.commit()

    recording_llm = _RecordingLlm()

    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(tmp_path / "skills" / str(ws_id))
        loader.load_all()
        return loader

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        frame = (run.payload or {}).get("frame") or {}
        return RunOrchestrator(
            session=session,
            llm=recording_llm,
            sandbox_manager=NoopSandboxManager(),
            suggested_skill=frame.get("skill_match"),
        )

    deps = AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_orchestrator_factory,
        workspace_root=tmp_path / "runs",
    )  # no frame_llm → keyword fallback
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY or run.status is RunStatus.RUNNING
        frame = run.payload["frame"]
        assert frame["skill_match"] is None
        assert frame["path_classification"] == "agent_loop"

    blob = "\n".join(
        m.get("content", "")
        for m in recording_llm.calls[0]["messages"]
        if isinstance(m.get("content"), str)
    ).lower()
    assert "suggested skill" not in blob
