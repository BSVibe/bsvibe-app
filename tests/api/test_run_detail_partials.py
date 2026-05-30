"""D6 delta 2 — /api/v1/runs/{id}/detail surfaces partials distinguished from the final.

Synthesis §13 — Deliver is a continuous side channel. The Run-view must list
every mid-loop partial Deliverable AS WELL AS the verified-final one, with the
two visually distinguished so the founder can tell "in-flight progress" from
"the run finished and verified this."

Asserted deltas:

* 2 mid-loop partials + 1 verified-final → ``partial_deliverables`` has 2
  entries, ``deliverable_id`` is the verified-final's id (NOT the latest partial).
* A run with 0 partials + 1 verified-final → ``partial_deliverables`` is empty,
  ``deliverable_id`` is the verified-final (back-compat unchanged).
* Each partial entry carries the founder-relevant fields: ``artifact_type``,
  ``summary``, ``channel`` (when set), ``created_at``, ``id``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.execution.verified_deliverable import PARTIAL_DELIVERABLE_KIND
from tests._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_run(s, *, run_id: uuid.UUID, ws: uuid.UUID, status: RunStatus) -> None:
    s.add(
        ExecutionRun(
            id=run_id,
            workspace_id=ws,
            status=status,
            payload={"intent_text": "ship release"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )
    await s.flush()


async def test_partials_listed_and_terminal_separated(configured_client, db, workspace_id) -> None:
    """2 mid-loop partials + 1 verified-final → API returns the 2 partials in a
    dedicated list, and ``deliverable_id`` is the verified-final's id.

    The verified-final is NEWER than the last partial; today's "latest deliverable"
    query happens to point at the same row, but with two unrelated partials this
    test pins the contract — ``deliverable_id`` must filter out partials and
    return the verified-final regardless of timing.
    """
    run_id = uuid.uuid4()
    partial_a = uuid.uuid4()
    partial_b = uuid.uuid4()
    final_id = uuid.uuid4()
    t0 = datetime.now(tz=UTC)

    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.SHIPPED)
        s.add(
            Deliverable(
                id=partial_a,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={
                    "kind": PARTIAL_DELIVERABLE_KIND,
                    "artifact_type": "pr",
                    "summary": "opened PR #1",
                    "channel": "github",
                    "external_ref": "github://acme/site/pull/1",
                },
                created_at=t0,
            )
        )
        s.add(
            Deliverable(
                id=partial_b,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PAGE,
                payload={
                    "kind": PARTIAL_DELIVERABLE_KIND,
                    "artifact_type": "page",
                    "summary": "updated runbook",
                    "channel": "notion",
                },
                created_at=t0 + timedelta(seconds=1),
            )
        )
        # Verified-final lands LAST in time + carries NO partial kind.
        s.add(
            Deliverable(
                id=final_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": ["marker"], "summary": "all done"},
                created_at=t0 + timedelta(seconds=2),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()

    # The verified-final is the one the founder taps for the Delivery Report.
    assert body["deliverable_id"] == str(final_id), (
        "deliverable_id must point to the verified-final, not a partial"
    )

    partials = body.get("partial_deliverables")
    assert isinstance(partials, list), "RunDetail must include partial_deliverables list"
    assert len(partials) == 2

    by_id = {p["id"]: p for p in partials}
    assert str(partial_a) in by_id
    assert str(partial_b) in by_id

    pa = by_id[str(partial_a)]
    assert pa["artifact_type"] == "pr"
    assert pa["summary"] == "opened PR #1"
    assert pa["channel"] == "github"
    assert "created_at" in pa


async def test_no_partials_unchanged_behavior(configured_client, db, workspace_id) -> None:
    """Back-compat: a run with NO partials + 1 verified-final still surfaces
    the verified-final on ``deliverable_id`` and an empty ``partial_deliverables``."""
    run_id = uuid.uuid4()
    final_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.SHIPPED)
        s.add(
            Deliverable(
                id=final_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": ["marker"], "summary": "done"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deliverable_id"] == str(final_id)
    assert body.get("partial_deliverables") == []
