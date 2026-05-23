"""/api/v1/deliverables — read API end-to-end (SQLite default, real PG on env).

Deliverables are *created* by the agent loop / workers, never via HTTP, so the
surface is read-only. These tests seed ``Deliverable`` rows (and the parent
``ExecutionRun`` the PG-enforced FK requires) and assert list/get behaviour:
newest-first ordering, workspace scoping, payload-field mapping, the optional
``run_id`` filter, the 404 for a cross-workspace id, and the limit cap.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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

from .._support import fake_current_user

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)

pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db():
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(ExecutionBase.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            await conn.run_sync(ExecutionBase.metadata.drop_all)
    await engine.dispose()


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


async def _seed_run(s, *, run_id: uuid.UUID, ws: uuid.UUID) -> None:
    """Create the parent ExecutionRun so the deliverables FK resolves (PG).

    Flush immediately: there is no ORM ``relationship()`` linking Deliverable to
    ExecutionRun (only a column-level FK), and the deliverables are inserted via
    a batched ``executemany`` — so the parent row must be flushed to the DB
    before the children or PG rejects the FK (SQLite silently tolerates it).
    """
    s.add(
        ExecutionRun(
            id=run_id,
            workspace_id=ws,
            status=RunStatus.SHIPPED,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )
    await s.flush()


async def test_list_newest_first_with_payload_mapping(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=older_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                artifact_uri="https://example.com/pr/1",
                payload={"summary": "first ship", "artifact_refs": ["pr#1"]},
                created_at=base,
            )
        )
        s.add(
            Deliverable(
                id=newer_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PAGE,
                artifact_uri=None,
                payload={"summary": "second ship", "artifact_refs": ["page-a", "page-b"]},
                created_at=base + timedelta(minutes=5),
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [str(newer_id), str(older_id)]

    newest = rows[0]
    assert newest["run_id"] == str(run_id)
    assert newest["workspace_id"] == str(workspace_id)
    assert newest["deliverable_type"] == "page"
    assert newest["summary"] == "second ship"
    assert newest["artifact_refs"] == ["page-a", "page-b"]
    assert newest["artifact_uri"] is None

    oldest = rows[1]
    assert oldest["summary"] == "first ship"
    assert oldest["artifact_refs"] == ["pr#1"]
    assert oldest["artifact_uri"] == "https://example.com/pr/1"


async def test_list_workspace_scoped(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    mine = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
        s.add(
            Deliverable(
                id=mine,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        # Another workspace's deliverable — MUST NOT appear.
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=other_run_id,
                workspace_id=other_ws,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(mine)


async def test_list_run_id_filter(configured_client, db, workspace_id) -> None:
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    in_a = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_a, ws=workspace_id)
        await _seed_run(s, run_id=run_b, ws=workspace_id)
        s.add(
            Deliverable(
                id=in_a,
                run_id=run_a,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "a"},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run_b,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "b"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables?run_id={run_a}")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(in_a)
    assert rows[0]["run_id"] == str(run_a)


async def test_get_by_id_and_cross_workspace_404(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
        s.add(
            Deliverable(
                id=mine,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "mine", "artifact_refs": []},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            Deliverable(
                id=theirs,
                run_id=other_run_id,
                workspace_id=other_ws,
                deliverable_type=DeliverableType.PR,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{mine}")
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "mine"

    # Cross-workspace id resolves to 404, not a leak.
    r2 = await configured_client.get(f"/api/v1/deliverables/{theirs}")
    assert r2.status_code == 404

    # Unknown id → 404.
    r3 = await configured_client.get(f"/api/v1/deliverables/{uuid.uuid4()}")
    assert r3.status_code == 404


async def test_list_empty(configured_client) -> None:
    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200
    assert r.json() == []


async def test_limit_capped(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        for _ in range(3):
            s.add(
                Deliverable(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()

    # Over-cap and under-floor limits are clamped, not errored.
    r = await configured_client.get("/api/v1/deliverables?limit=99999")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 3

    r2 = await configured_client.get("/api/v1/deliverables?limit=1")
    assert r2.status_code == 200
    assert len(r2.json()) == 1
