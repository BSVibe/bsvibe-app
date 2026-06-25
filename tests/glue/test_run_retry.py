"""L2 (#9) — failed / stood-down runs are recoverable, not dead-ends.

The founder pain: a run that ends ``failed`` / ``cancelled`` shows
"완료하지 못했어요. 지금 하실 일은 없어요." with no explanation and no way
forward — a trust-killer. This lift restores recovery:

* ``POST /api/v1/runs/{id}/retry`` re-opens a TERMINAL-failed run (FAILED /
  CANCELLED → OPEN) so ``AgentWorker.drive_once`` re-picks it for another
  attempt. Non-terminal runs → 409; cross-workspace / unknown → 404.
* ``GET /api/v1/runs/{id}/detail`` surfaces the latest failure ``reason`` (from
  ExecutionRunHistory) so the founder sees WHY, not a generic dead-end.
* A ``verification_failed`` / ``human_review_required`` Decision gains a
  one-click ``retry`` action that re-opens the paused run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)

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


async def _seed_run(
    sf_: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    status: RunStatus,
    *,
    failure_reason: str | None = None,
) -> uuid.UUID:
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
        await s.flush()
        if failure_reason is not None:
            s.add(
                ExecutionRunHistory(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    workspace_id=workspace_id,
                    from_status=RunStatus.RUNNING,
                    to_status=status,
                    reason=failure_reason,
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()
        return run.id


# --------------------------------------------------------------------------
# POST /runs/{id}/retry — re-open a terminal-failed run
# --------------------------------------------------------------------------


async def test_retry_failed_run_reopens(client, sf, workspace_id) -> None:
    run_id = await _seed_run(sf, workspace_id, RunStatus.FAILED, failure_reason="boom")

    resp = await client.post(f"/api/v1/runs/{run_id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.OPEN
        # A retry marker is recorded so the loop / observability can see it.
        assert int(run.payload.get("retry_count", 0)) == 1


async def test_retry_cancelled_run_reopens(client, sf, workspace_id) -> None:
    run_id = await _seed_run(sf, workspace_id, RunStatus.CANCELLED)
    resp = await client.post(f"/api/v1/runs/{run_id}/retry")
    assert resp.status_code == 200, resp.text
    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.OPEN


async def test_retry_records_history(client, sf, workspace_id) -> None:
    from sqlalchemy import select

    run_id = await _seed_run(sf, workspace_id, RunStatus.FAILED, failure_reason="boom")
    await client.post(f"/api/v1/runs/{run_id}/retry")
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(ExecutionRunHistory).where(ExecutionRunHistory.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        # The original failure + the retry re-open transition.
        assert any(h.to_status is RunStatus.OPEN for h in rows)


@pytest.mark.parametrize(
    "status",
    [RunStatus.RUNNING, RunStatus.OPEN, RunStatus.REVIEW_READY, RunStatus.SHIPPED],
)
async def test_retry_non_terminal_run_409(client, sf, workspace_id, status) -> None:
    run_id = await _seed_run(sf, workspace_id, status)
    resp = await client.post(f"/api/v1/runs/{run_id}/retry")
    assert resp.status_code == 409, resp.text


async def test_retry_cross_workspace_404(client, sf) -> None:
    run_id = await _seed_run(sf, uuid.uuid4(), RunStatus.FAILED)
    resp = await client.post(f"/api/v1/runs/{run_id}/retry")
    assert resp.status_code == 404


async def test_retry_unknown_run_404(client) -> None:
    resp = await client.post(f"/api/v1/runs/{uuid.uuid4()}/retry")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# GET /runs/{id}/detail — surface the failure reason (not a blank dead-end)
# --------------------------------------------------------------------------


async def test_run_detail_surfaces_failure_reason(client, sf, workspace_id) -> None:
    run_id = await _seed_run(
        sf, workspace_id, RunStatus.FAILED, failure_reason="sandbox could not start"
    )
    resp = await client.get(f"/api/v1/runs/{run_id}/detail")
    assert resp.status_code == 200, resp.text
    assert resp.json()["failure_reason"] == "sandbox could not start"


async def test_run_detail_no_failure_reason_when_running(client, sf, workspace_id) -> None:
    run_id = await _seed_run(sf, workspace_id, RunStatus.RUNNING)
    resp = await client.get(f"/api/v1/runs/{run_id}/detail")
    assert resp.status_code == 200, resp.text
    assert resp.json()["failure_reason"] is None


# --------------------------------------------------------------------------
# Decision retry action — re-open a paused verification_failed run
# --------------------------------------------------------------------------


async def test_verification_failed_decision_offers_retry_action(client, sf, workspace_id) -> None:
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="verification_failed",
            payload={"reason": "tests did not pass"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    listing = await client.get("/api/v1/checkpoints")
    assert listing.status_code == 200
    item = next(i for i in listing.json() if i["id"] == str(decision_id))
    keys = {a["key"] for a in (item.get("actions") or [])}
    assert "retry" in keys


async def test_resolve_verification_failed_with_retry_reopens(client, sf, workspace_id) -> None:
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        run_id = run.id
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="verification_failed",
            payload={"reason": "tests did not pass"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    resp = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"action_key": "retry"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_status"] == "open"

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.OPEN
        decision = await s.get(Decision, decision_id)
        assert decision is not None and decision.status is DecisionStatus.RESOLVED
