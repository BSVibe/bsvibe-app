"""Direct path end-to-end — POST /api/v1/messages → delivered artifact.

This is the Phase 1 exit / Phase 2 entry proof (Workflow §11.1). It wires
the whole Direct chain and ticks every DB-polling worker exactly once
(single-tick methods, never the infinite poll loops):

    POST /api/v1/messages
      → DirectTrigger.submit              → TriggerEventRow
      → IntakeWorker.drain_once           → RequestRow (OPEN)
      → AgentWorker.claim_once            → ExecutionRun (OPEN) + Request RUNNING
      → AgentWorker.drive_once            → FrameStage.frame + AgentRunner.drive
          → RunOrchestrator.run (verified)→ ExecutionRun REVIEW_READY
                                          + Deliverable + DeliveryEventRow
      → DeliveryWorker.drain_once         → dispatched to the in-test sink

The work LLM is the deterministic ``ScriptedLlm`` from the orchestrator
unit tests; the sandbox is the host-side ``NoopSandboxManager`` (no Docker,
no real model); the delivery sink is an in-test ``PluginDispatchAdapter``.
Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.delivery.db import DeliveryEventRow
from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import Deliverable, ExecutionRun, RunStatus
from backend.execution.orchestrator import LoopToolCall, LoopTurn, RunOrchestrator
from backend.intake.db import RequestRow, RequestStatus, TriggerEventRow
from backend.skills.loader import SkillLoader
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workers.agent_worker import AgentExecutionDeps, AgentWorker
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workers.intake_worker import IntakeWorker

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """A deterministic ``LoopLlm`` — pops the next pre-programmed turn FIFO."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


class _SinkDispatcher:
    """An in-test ``PluginDispatchAdapter`` — records what was dispatched."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.dispatched.append(kwargs)
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="sink", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _scripted_verified_run() -> _ScriptedLlm:
    """Program the work LLM: declare a command check + write the artifact,
    then return plain text — which triggers verify → ``verified``."""
    return _ScriptedLlm(
        [
            LoopTurn(
                content="Writing the deliverable and declaring how to check it.",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "test -f answer.txt"}],
                    ),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done — answer.txt written.", tool_calls=()),
        ]
    )


def _execution_deps(
    sf_: async_sessionmaker[AsyncSession], workspace_root: Path
) -> AgentExecutionDeps:
    llm = _scripted_verified_run()

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
    )


@pytest_asyncio.fixture
async def client(sf, founder_id: uuid.UUID, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


# --------------------------------------------------------------------------
# The end-to-end Direct path
# --------------------------------------------------------------------------


async def test_direct_path_message_to_delivered_artifact(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    # 1. Founder POSTs a direct message → TriggerEvent (source=direct).
    resp = await client.post("/api/v1/messages", json={"text": "build the answer file"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body == {"accepted": True, "duplicate": False, "workspace_id": str(workspace_id)}

    async with sf() as s:
        triggers = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(triggers) == 1
    assert triggers[0].source == "direct"

    # 2. IntakeWorker drains the TriggerEvent → Request (OPEN).
    intake = IntakeWorker(session_factory=sf)
    assert await intake.drain_once() == 1
    async with sf() as s:
        requests = (await s.execute(select(RequestRow))).scalars().all()
    assert len(requests) == 1
    assert requests[0].status is RequestStatus.OPEN
    assert requests[0].payload.get("text") == "build the answer file"

    # 3. AgentWorker claims the Request → ExecutionRun (OPEN) + Request RUNNING.
    deps = _execution_deps(sf, tmp_path)
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.claim_once() == 1
    async with sf() as s:
        run = (await s.execute(select(ExecutionRun))).scalar_one()
        req = await s.get(RequestRow, requests[0].id)
        assert req is not None and req.status is RequestStatus.RUNNING
        assert run.status is RunStatus.OPEN
        run_id = run.id

    # 4. AgentWorker frames + drives the loop → REVIEW_READY + Deliverable + DeliveryEvent.
    assert await agent.drive_once() == 1
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.REVIEW_READY
        assert run.payload.get("intent_text") == "build the answer file"
        assert "frame" in run.payload

        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        assert deliverable.run_id == run_id
        assert "answer.txt" in (deliverable.payload.get("artifact_refs") or [])

        deliver_event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert deliver_event.deliverable_id == deliverable.id
        deliverable_id = deliverable.id

    # The work LLM actually wrote the artifact to the run's workspace.
    assert (tmp_path / str(run_id) / "answer.txt").read_text() == "42\n"

    # 5. DeliveryWorker drains the DeliveryEvent → dispatched to the sink (no Safe Mode).
    sink = _SinkDispatcher()
    delivery = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await delivery.drain_once() == 1
    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["deliverable_id"] == deliverable_id
    assert sink.dispatched[0]["workspace_id"] == workspace_id

    # Event removed from the queue after dispatch.
    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


async def test_direct_path_duplicate_submit_collapses(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Same founder + same text twice → second POST is reported as a duplicate
    and lands no second TriggerEvent (DirectTrigger idempotency)."""
    first = await client.post("/api/v1/messages", json={"text": "same request"})
    assert first.status_code == 202
    assert first.json()["duplicate"] is False

    second = await client.post("/api/v1/messages", json={"text": "same request"})
    assert second.status_code == 202
    assert second.json()["duplicate"] is True

    async with sf() as s:
        triggers = (await s.execute(select(TriggerEventRow))).scalars().all()
    assert len(triggers) == 1


async def test_messages_rejects_empty_text(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/messages", json={"text": ""})
    assert resp.status_code == 422


async def test_intake_worker_idle_returns_zero(sf: async_sessionmaker[AsyncSession]) -> None:
    assert await IntakeWorker(session_factory=sf).drain_once() == 0


async def test_agent_worker_drive_once_noop_without_execution(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Without an execution backend the worker can only stage runs."""
    assert await AgentWorker(session_factory=sf).drive_once() == 0
