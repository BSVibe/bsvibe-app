"""AgentWorker × ProductTickPlanner wiring — the glass-box intent override.

A ``product_tick`` run with a ``product_id`` is handed to the injected
:class:`ProductTickPlanner` (via ``AgentExecutionDeps.tick_planner_for``, DI that
mirrors ``frame_llm``) at frame time. When the planner returns a ``TickPlan`` the
worker OVERRIDES the framing intent with the concrete instruction (so framing
classifies the real task) and stashes the plan as glass-box provenance on
``run.payload["tick_plan"]``. When the planner returns ``None`` NOTHING changes —
the static meta-instruction remains the fallback. A non-tick run never touches
the planner.

Plus a regression test that LOCKS the redis fix: the production factory
``build_agent_execution_deps`` must thread its ``redis_client`` into the planner
(an executor-account frame route needs redis, else the planner is a live no-op).

The planner is a STUB injected as ``tick_planner_for`` to isolate the wiring;
the frame LLM is a recording stub so we can prove the concrete instruction
reached framing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.application.product_tick_planner import ProductTickPlanner, TickPlan
from backend.workflow.application.runtime.agent_runtime import build_agent_execution_deps
from backend.workflow.infrastructure.db import ExecutionRun
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

_STATIC_INSTRUCTION = "This is an autonomous product tick. Decide + do the next action."


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _RecordingFrameLlm:
    """Records the framing ``user`` prompt; returns a valid agent_loop frame."""

    def __init__(self) -> None:
        self.users: list[str] = []

    async def complete_text(self, *, system: str, user: str) -> str:
        self.users.append(user)
        return json.dumps(
            {
                "framed_intent": "framed",
                "skill_match": None,
                "artifact_type_hint": "code",
                "path_classification": "agent_loop",
                "pipeline": "single",
            }
        )


class _StubPlanner:
    """Injected in place of the real planner. Records every ``plan`` call to
    prove (non-)invocation and returns a pre-set ``result``."""

    def __init__(self, result: TickPlan | None) -> None:
        self.result = result
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def plan(self, *, workspace_id: uuid.UUID, product_id: uuid.UUID) -> TickPlan | None:
        self.calls.append((workspace_id, product_id))
        return self.result


async def _seed_tick_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID | None,
    kind: str | None,
) -> uuid.UUID:
    trigger = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        source="schedule",
        trigger_kind=TriggerKind.SCHEDULE,
        idempotency_key=f"k-{uuid.uuid4()}",
        payload={"text": _STATIC_INSTRUCTION},
        received_at=datetime.now(tz=UTC),
    )
    session.add(trigger)
    await session.flush()
    payload: dict[str, Any] = {"text": _STATIC_INSTRUCTION}
    if kind is not None:
        payload["kind"] = kind
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trigger_event_id=trigger.id,
        product_id=product_id,
        status=RequestStatus.RUNNING,
        payload=payload,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(request)
    await session.flush()
    return await AgentRunner(session).open_run(request=request)


def _deps(
    tmp_path: Path, frame_llm: _RecordingFrameLlm, planner: _StubPlanner
) -> AgentExecutionDeps:
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(tmp_path / "skills" / str(ws_id))
        loader.load_all()
        return loader

    # Returning None from the factory pauses the run right after framing — enough
    # to inspect the framing override without exercising the whole drive loop.
    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> None:
        return None

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_orchestrator_factory,
        workspace_root=tmp_path / "runs",
        frame_llm=frame_llm,
        tick_planner_for=lambda session: planner,
    )


async def test_tick_plan_overrides_framing_intent_and_stashes_provenance(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    plan = TickPlan(
        instruction="Add the Stripe webhook signature check",
        rationale="prior run left verification open",
    )
    planner = _StubPlanner(result=plan)

    async with sf() as session:
        run_id = await _seed_tick_run(
            session, workspace_id=workspace_id, product_id=product_id, kind="product_tick"
        )
        await session.commit()

    frame_llm = _RecordingFrameLlm()
    agent = AgentWorker(session_factory=sf, execution=_deps(tmp_path, frame_llm, planner))
    assert await agent.drive_once() == 1

    # The planner was invoked for this product.
    assert planner.calls == [(workspace_id, product_id)]
    # The framing saw the CONCRETE instruction (override reached the frame stage).
    # The frame stage lowercases its extracted text, so compare case-insensitively.
    assert any("add the stripe webhook signature check" in u.lower() for u in frame_llm.users)

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.payload["intent_text"] == "Add the Stripe webhook signature check"
        assert run.payload["tick_plan"] == {
            "instruction": "Add the Stripe webhook signature check",
            "rationale": "prior run left verification open",
        }


async def test_planner_none_falls_back_to_static_instruction(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    planner = _StubPlanner(result=None)  # planner declines

    async with sf() as session:
        run_id = await _seed_tick_run(
            session, workspace_id=workspace_id, product_id=product_id, kind="product_tick"
        )
        await session.commit()

    frame_llm = _RecordingFrameLlm()
    agent = AgentWorker(session_factory=sf, execution=_deps(tmp_path, frame_llm, planner))
    assert await agent.drive_once() == 1

    assert planner.calls == [(workspace_id, product_id)]  # planner WAS consulted
    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        # No override → the static meta-instruction remains the framing intent.
        assert run.payload["intent_text"] == _STATIC_INSTRUCTION
        assert "tick_plan" not in run.payload


async def test_non_tick_run_never_invokes_planner(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    # A plan is armed, but the run is NOT a product_tick → it must never be used.
    planner = _StubPlanner(result=TickPlan(instruction="should not apply", rationale="x"))

    async with sf() as session:
        run_id = await _seed_tick_run(
            session, workspace_id=workspace_id, product_id=product_id, kind=None
        )
        await session.commit()

    frame_llm = _RecordingFrameLlm()
    agent = AgentWorker(session_factory=sf, execution=_deps(tmp_path, frame_llm, planner))
    assert await agent.drive_once() == 1

    assert planner.calls == []  # planner NEVER consulted for a non-tick run
    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.payload["intent_text"] == _STATIC_INSTRUCTION
        assert "tick_plan" not in run.payload


async def test_build_deps_threads_redis_into_tick_planner() -> None:
    """Regression lock: the production factory MUST thread its ``redis_client``
    (and settings) into the planner it builds. An executor-account frame route
    needs redis for its worker-stream XADD; without it the planner resolves
    ``CALLER_FRAME`` differently from the frame stage, silently fails, and the
    tick degrades to the static meta-instruction — a LIVE no-op that would stay
    test-green. This test fails the instant redis stops being threaded."""
    settings = get_settings()
    sentinel = object()
    deps = build_agent_execution_deps(
        settings=settings,
        sandbox_manager=NoopSandboxManager(),
        redis_client=sentinel,
    )
    assert deps.tick_planner_for is not None
    # The session arg is only stored by the constructor; a placeholder suffices.
    planner = deps.tick_planner_for(SimpleNamespace())
    assert isinstance(planner, ProductTickPlanner)
    assert planner._redis is sentinel  # the redis transport was threaded through
    assert planner._settings is settings
