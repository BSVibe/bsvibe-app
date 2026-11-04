"""``verification_rounds`` / ``passed_on_round`` on the MCP run tools, and the
first-round pass-rate tool. Real rows, real handlers (through the registry)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine
from ._rls import scoped_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]

F, P = VerificationOutcome.FAILED, VerificationOutcome.PASSED


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> None:
    async with scoped_session(db, workspace_id) as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws"))
        await s.commit()


def _ctx(s, workspace_id) -> ToolContext:
    principal = McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset({"mcp:read"}),
        jti=uuid.uuid4(),
    )
    return ToolContext(principal=principal, session=s)


async def _seed_run(db, workspace_id, outcomes, *, status=RunStatus.SHIPPED, age=0) -> uuid.UUID:
    run_id = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(hours=age)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=status,
                payload={},
                created_at=base,
                updated_at=base,
            )
        )
        await s.flush()
        for i, outcome in enumerate(outcomes):
            s.add(
                VerificationResult(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    outcome=outcome,
                    contract={},
                    result={},
                    created_at=base + timedelta(minutes=i),
                )
            )
        await s.commit()
    return run_id


async def _call(db, registry, workspace_id, tool, args):
    async with db() as s:
        return await registry.call_tool(tool, args, _ctx(s, workspace_id))


async def test_failed_failed_passed_reads_three_rounds_passed_on_third(
    db, workspace_id, registry, seeded
) -> None:
    run_id = await _seed_run(db, workspace_id, [F, F, P])

    listed = await _call(db, registry, workspace_id, "bsvibe_runs_list", {})
    detail = await _call(db, registry, workspace_id, "bsvibe_runs_detail", {"run_id": str(run_id)})

    assert [(r["verification_rounds"], r["passed_on_round"]) for r in listed] == [(3, 3)]
    assert (detail["verification_rounds"], detail["passed_on_round"]) == (3, 3)


async def test_run_without_verification_reads_zero_rounds_and_null(
    db, workspace_id, registry, seeded
) -> None:
    run_id = await _seed_run(db, workspace_id, [])

    listed = await _call(db, registry, workspace_id, "bsvibe_runs_list", {})
    detail = await _call(db, registry, workspace_id, "bsvibe_runs_detail", {"run_id": str(run_id)})

    assert (listed[0]["verification_rounds"], listed[0]["passed_on_round"]) == (0, None)
    assert (detail["verification_rounds"], detail["passed_on_round"]) == (0, None)


async def test_first_round_pass_rate_counts_only_this_workspaces_verified_runs(
    db, workspace_id, registry, seeded
) -> None:
    other = uuid.uuid4()
    async with scoped_session(db, other) as s:
        s.add(WorkspaceRow(id=other, name="other"))
        await s.commit()

    await _seed_run(db, workspace_id, [P], age=0)  # first round
    await _seed_run(db, workspace_id, [P], age=1)  # first round
    await _seed_run(db, workspace_id, [F, F, P], age=2)  # third round
    await _seed_run(db, workspace_id, [F], status=RunStatus.FAILED, age=3)  # never passed
    await _seed_run(db, workspace_id, [], age=4)  # never verified
    await _seed_run(db, workspace_id, [P], status=RunStatus.RUNNING, age=5)  # not finished
    await _seed_run(db, other, [P], age=0)  # someone else's
    await _seed_run(db, other, [P], age=1)

    out = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {})

    assert out["passed_first_round"] == 2
    assert out["runs_verified"] == 4
    assert out["runs_considered"] == 5
    assert out["pass_rate"] == 0.5


async def test_first_round_pass_rate_looks_only_at_the_latest_n_runs(
    db, workspace_id, registry, seeded
) -> None:
    await _seed_run(db, workspace_id, [F, P], age=0)
    await _seed_run(db, workspace_id, [P], age=1)
    await _seed_run(db, workspace_id, [P], age=2)

    out = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {"limit": 2})

    assert (out["passed_first_round"], out["runs_verified"]) == (1, 2)


async def test_first_round_pass_rate_is_null_when_nothing_was_verified(
    db, workspace_id, registry, seeded
) -> None:
    await _seed_run(db, workspace_id, [])

    out = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {})

    assert (out["passed_first_round"], out["runs_verified"], out["pass_rate"]) == (0, 0, None)


async def test_rest_and_mcp_first_round_pass_rate_agree_and_product_filter_is_scoped(
    db, workspace_id, registry, seeded
) -> None:
    import httpx

    from backend.api.deps import get_current_user, get_db_session, get_workspace_id
    from backend.api.main import create_app

    from .._support import fake_current_user, publishing_workspace

    mine, theirs = uuid.uuid4(), uuid.uuid4()
    first = await _seed_run(db, workspace_id, [P], age=0)
    second = await _seed_run(db, workspace_id, [F, P], age=1)
    other_product = await _seed_run(db, workspace_id, [P], age=2)
    await _seed_run(db, workspace_id, [], age=3)
    async with scoped_session(db, workspace_id) as s:
        for rid, pid in ((first, mine), (second, mine), (other_product, theirs)):
            run = await s.get(ExecutionRun, rid)
            run.product_id = pid
        await s.commit()

    mcp = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {})

    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        rest = (await c.get("/api/v1/runs/first-round-pass-rate?limit=20")).json()
        scoped = (await c.get(f"/api/v1/runs/first-round-pass-rate?product_id={mine}")).json()

    assert rest == mcp
    assert (rest["passed_first_round"], rest["runs_verified"]) == (2, 3)
    # the other product's first-round pass is not counted
    assert (scoped["passed_first_round"], scoped["runs_verified"]) == (1, 2)
    assert scoped["runs_considered"] == 2


async def test_first_round_pass_rate_defaults_to_30_runs_on_rest_and_mcp(
    db, workspace_id, registry, seeded
) -> None:
    import httpx

    from backend.api.deps import get_current_user, get_db_session, get_workspace_id
    from backend.api.main import create_app

    from .._support import fake_current_user, publishing_workspace

    for age in range(31):
        await _seed_run(db, workspace_id, [P], age=age)

    mcp = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {})

    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        rest = (await c.get("/api/v1/runs/first-round-pass-rate")).json()
        too_big = await c.get("/api/v1/runs/first-round-pass-rate?limit=201")
        zero = await c.get("/api/v1/runs/first-round-pass-rate?limit=0")

    assert rest["runs_considered"] == 30
    assert mcp["runs_considered"] == 30
    assert too_big.status_code == 422
    assert zero.status_code == 422


async def test_rest_and_mcp_agree_for_the_same_product_id(
    db, workspace_id, registry, seeded
) -> None:
    import httpx

    from backend.api.deps import get_current_user, get_db_session, get_workspace_id
    from backend.api.main import create_app

    from .._support import fake_current_user, publishing_workspace

    mine, theirs = uuid.uuid4(), uuid.uuid4()
    a = await _seed_run(db, workspace_id, [P], age=0)
    b = await _seed_run(db, workspace_id, [F, P], age=1)
    c = await _seed_run(db, workspace_id, [F, F, P], age=2)
    d = await _seed_run(db, workspace_id, [P], age=3)
    async with scoped_session(db, workspace_id) as s:
        for rid, pid in ((a, mine), (b, mine), (c, mine), (d, theirs)):
            run = await s.get(ExecutionRun, rid)
            run.product_id = pid
        await s.commit()

    mcp = await _call(
        db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {"product_id": str(mine)}
    )
    whole = await _call(db, registry, workspace_id, "bsvibe_runs_first_round_pass_rate", {})

    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rest = (await client.get(f"/api/v1/runs/first-round-pass-rate?product_id={mine}")).json()

    assert rest == mcp
    assert (mcp["passed_first_round"], mcp["runs_verified"]) == (1, 3)
    # without product_id the whole workspace is counted
    assert (whole["passed_first_round"], whole["runs_verified"]) == (2, 4)
