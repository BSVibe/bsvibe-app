"""``verification_rounds`` / ``passed_on_round`` on the REST run list + detail.

Real runs and real ``VerificationResult`` rows go into the database and the
real handlers read them back — nothing here builds the response by hand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.workflow.infrastructure.db import (
    ExecutionBase,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user, publishing_workspace
from .conftest import commit_per_workspace

pytestmark = pytest.mark.asyncio

F, P = VerificationOutcome.FAILED, VerificationOutcome.PASSED


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id: uuid.UUID):
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(db, workspace_id, outcomes, results=None) -> uuid.UUID:
    run_id = uuid.uuid4()
    base = datetime.now(UTC)
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.SHIPPED,
                payload={},
                created_at=base,
                updated_at=base,
            )
        )
        for i, outcome in enumerate(outcomes):
            s.add(
                VerificationResult(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=workspace_id,
                    outcome=outcome,
                    contract={},
                    result=results[i] if results else {},
                    created_at=base + timedelta(minutes=i),
                )
            )
        await commit_per_workspace(s)
    return run_id


async def test_failed_failed_passed_is_three_rounds_passed_on_third(
    client, db, workspace_id
) -> None:
    run_id = await _seed(db, workspace_id, [F, F, P])

    listed = (await client.get("/api/v1/runs")).json()
    detail = (await client.get(f"/api/v1/runs/{run_id}/detail")).json()
    shown = (await client.get(f"/api/v1/runs/{run_id}")).json()

    assert [(r["verification_rounds"], r["passed_on_round"]) for r in listed] == [(3, 3)]
    assert (detail["verification_rounds"], detail["passed_on_round"]) == (3, 3)
    assert (shown["verification_rounds"], shown["passed_on_round"]) == (3, 3)


async def test_run_without_verification_has_zero_rounds_and_null_pass(
    client, db, workspace_id
) -> None:
    run_id = await _seed(db, workspace_id, [])

    listed = (await client.get("/api/v1/runs")).json()
    detail = (await client.get(f"/api/v1/runs/{run_id}/detail")).json()

    assert (listed[0]["verification_rounds"], listed[0]["passed_on_round"]) == (0, None)
    assert (detail["verification_rounds"], detail["passed_on_round"]) == (0, None)


async def test_never_passed_run_has_rounds_but_null_pass(client, db, workspace_id) -> None:
    await _seed(db, workspace_id, [F, F])

    listed = (await client.get("/api/v1/runs")).json()

    assert (listed[0]["verification_rounds"], listed[0]["passed_on_round"]) == (2, None)


async def test_list_reads_every_runs_rounds_in_one_query(
    client, db, workspace_id, monkeypatch
) -> None:
    from sqlalchemy import event

    for outcomes in ([P], [F, P], [F, F, P], []):
        await _seed(db, workspace_id, outcomes)

    statements: list[str] = []
    engine = db.kw["bind"]
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cur, stmt, *a: statements.append(stmt),
    )
    listed = (await client.get("/api/v1/runs")).json()

    assert sorted(r["verification_rounds"] for r in listed) == [0, 1, 2, 3]
    assert sum("verification_results" in s for s in statements) == 1


def _ledger_result(*rows: tuple[str, str, str, bool], failed_cmd: str | None = None) -> dict:
    result: dict = {
        "requirements": [
            {
                "id": i,
                "text": "t",
                "kind": "do",
                "status": status,
                "note": note,
                "evidence_missing": missing,
            }
            for i, status, note, missing in rows
        ]
    }
    if failed_cmd:
        result["derived_gate"] = {"commands": [{"command": failed_cmd, "status": "failed"}]}
    return result


async def test_detail_lists_every_round_with_its_failed_checks_and_open_items(
    client, db, workspace_id
) -> None:
    run_id = await _seed(
        db,
        workspace_id,
        [F, F, P],
        [
            _ledger_result(("R1", "missing", "no evidence", False), ("R2", "done", "", False)),
            _ledger_result(("R1", "rejected", "cut off", True), failed_cmd="pytest -q"),
            _ledger_result(("R1", "done", "", False), ("R2", "done", "", False)),
        ],
    )

    detail = (await client.get(f"/api/v1/runs/{run_id}/detail")).json()

    rounds = detail["rounds"]
    assert [(r["round"], r["outcome"]) for r in rounds] == [
        (1, "failed"),
        (2, "failed"),
        (3, "passed"),
    ]
    assert [(i["id"], i["status"], i["note"]) for i in rounds[0]["open_items"]] == [
        ("R1", "missing", "no evidence")
    ]
    assert rounds[1]["failed_checks"] == ["pytest -q"]
    assert rounds[1]["open_items"][0]["evidence_missing"] is True
    assert rounds[2]["open_items"] == []
    # the latest-round ``verification`` is unchanged
    assert detail["verification"]["outcome"] == "passed"
