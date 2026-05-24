"""/api/v1/deliverables — read API end-to-end (SQLite default, real PG on env).

Deliverables are *created* by the agent loop / workers, never via HTTP, so the
surface is read-only. These tests seed ``Deliverable`` rows (and the parent
``ExecutionRun`` the PG-enforced FK requires) and assert list/get behaviour:
newest-first ordering, workspace scoping, payload-field mapping, the optional
``run_id`` filter, the 404 for a cross-workspace id, and the limit cap.
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
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user

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


async def test_report_returns_deliverable_with_verification(
    configured_client, db, workspace_id
) -> None:
    """The report bundles the deliverable + the VerificationResult rows for its
    run — each carrying outcome / contract / result, the "how BSVibe checked
    this" proof."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    contract = {
        "checks": [
            {"kind": "command", "command": "pytest -q", "rationale": "tests pass"},
            {"kind": "judge", "criteria": ["reads cleanly"], "rationale": "style"},
        ]
    }
    result = {"checks": [{"passed": True, "output": "19 passed"}]}
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                artifact_uri="https://github.com/acme/repo/pull/15",
                diff_url="https://github.com/acme/repo/commit/abc",
                payload={"summary": "Add getRelatedPosts", "artifact_refs": ["src/posts.ts"]},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                work_step_id=None,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract=contract,
                result=result,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()

    d = body["deliverable"]
    assert d["id"] == str(deliverable_id)
    assert d["summary"] == "Add getRelatedPosts"
    assert d["artifact_refs"] == ["src/posts.ts"]
    assert d["artifact_uri"] == "https://github.com/acme/repo/pull/15"
    assert d["diff_url"] == "https://github.com/acme/repo/commit/abc"
    assert d["deliverable_type"] == "pr"

    assert len(body["verifications"]) == 1
    v = body["verifications"][0]
    assert v["outcome"] == "passed"
    assert v["contract"] == contract
    assert v["result"] == result


async def test_report_empty_verification_does_not_error(
    configured_client, db, workspace_id
) -> None:
    """A run with no VerificationResult yields a calm empty list, not a 500."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "direct"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deliverable"]["id"] == str(deliverable_id)
    assert body["verifications"] == []


async def test_report_cross_workspace_404(configured_client, db, workspace_id) -> None:
    """A deliverable in another workspace's report is 404, never a leak."""
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
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

    r = await configured_client.get(f"/api/v1/deliverables/{theirs}/report")
    assert r.status_code == 404

    r2 = await configured_client.get(f"/api/v1/deliverables/{uuid.uuid4()}/report")
    assert r2.status_code == 404


async def test_report_only_includes_own_run_verifications(
    configured_client, db, workspace_id
) -> None:
    """Verification rows are scoped to the deliverable's run, not all of them."""
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        # A verification for an unrelated run — MUST NOT appear.
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=other_run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    verifications = r.json()["verifications"]
    assert len(verifications) == 1
    assert verifications[0]["outcome"] == "passed"


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
