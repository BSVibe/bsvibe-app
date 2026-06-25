"""L9 — the founder can STOP an in-flight run, and a retry resets the clock.

Two run-control gaps the founder hit:
* No way to cancel a RUNNING / OPEN task — only a paused decision could be
  discarded. ``POST /api/v1/runs/{id}/cancel`` flips an in-flight run to
  CANCELLED; a guard stops the worker from un-cancelling it.
* Elapsed time counted from the FIRST start even after a retry — retry now
  stamps ``restarted_at`` so the surface resets the clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(sf, founder_id: uuid.UUID, workspace_id: uuid.UUID):
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_current_user_row] = lambda: SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(sf_, workspace_id, status: RunStatus) -> uuid.UUID:
    async with sf_() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=status,
            payload={"text": "build the thing"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.commit()
        return run.id


# --------------------------------------------------------------------------
# POST /runs/{id}/cancel
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.OPEN])
async def test_cancel_inflight_run(client, sf, workspace_id, status) -> None:
    run_id = await _seed(sf, workspace_id, status)
    resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.CANCELLED


@pytest.mark.parametrize("status", [RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_cancel_terminal_run_409(client, sf, workspace_id, status) -> None:
    run_id = await _seed(sf, workspace_id, status)
    resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert resp.status_code == 409, resp.text


async def test_cancel_cross_workspace_404(client, sf) -> None:
    run_id = await _seed(sf, uuid.uuid4(), RunStatus.RUNNING)
    resp = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert resp.status_code == 404


async def test_cancel_unknown_404(client) -> None:
    resp = await client.post(f"/api/v1/runs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Cooperative cancel — the worker must not un-cancel a cancelled run
# --------------------------------------------------------------------------


async def test_worker_cannot_uncancel(sf, workspace_id) -> None:
    """A RUNNING run cancelled mid-drive must NOT be flipped back to a terminal
    success by the worker's post-drive transition (transition guard)."""
    run_id = await _seed(sf, workspace_id, RunStatus.RUNNING)
    async with sf() as s:
        runner = AgentRunner(s)
        # Founder cancels.
        await runner.transition(run_id=run_id, to_status=RunStatus.CANCELLED, reason="founder")
        # Worker's drive completes and tries to mark it review_ready — must no-op.
        moved = await runner.transition(run_id=run_id, to_status=RunStatus.REVIEW_READY)
        await s.commit()
        assert moved is False
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.CANCELLED


async def test_retry_reopens_a_cancelled_run(sf, workspace_id) -> None:
    """The cancel guard must STILL allow the explicit retry path (CANCELLED → OPEN)."""
    run_id = await _seed(sf, workspace_id, RunStatus.CANCELLED)
    async with sf() as s:
        runner = AgentRunner(s)
        moved = await runner.transition(run_id=run_id, to_status=RunStatus.OPEN, reason="retry")
        await s.commit()
        assert moved is True
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.OPEN


# --------------------------------------------------------------------------
# L9b — retry stamps restarted_at (the elapsed clock resets)
# --------------------------------------------------------------------------


async def test_retry_stamps_restarted_at(client, sf, workspace_id) -> None:
    run_id = await _seed(sf, workspace_id, RunStatus.FAILED)
    resp = await client.post(f"/api/v1/runs/{run_id}/retry")
    assert resp.status_code == 200, resp.text
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert isinstance(run.payload.get("restarted_at"), str) and run.payload["restarted_at"]


async def test_run_response_exposes_restarted_at(client, sf, workspace_id) -> None:
    run_id = await _seed(sf, workspace_id, RunStatus.FAILED)
    await client.post(f"/api/v1/runs/{run_id}/retry")
    detail = await client.get(f"/api/v1/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["restarted_at"] is not None
