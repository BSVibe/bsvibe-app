"""``bsvibe_runs_verification_quality`` and ``GET /api/v1/runs/verification-quality``.

Real runs and ``VerificationResult`` rows (ledger + counterfactual results
inside), read back through the real MCP registry and the real REST app.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

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
TOOL = "bsvibe_runs_verification_quality"


def _req(status: str, counterfactual: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"id": uuid.uuid4().hex[:4], "text": "t", "kind": "do", "status": status}
    if counterfactual:
        row["counterfactual"] = counterfactual
    return row


def _result(*ledger: dict[str, Any], failed_command: bool = False) -> dict[str, Any]:
    commands = [{"command": "pytest", "status": "failed" if failed_command else "passed"}]
    return {"derived_gate": {"commands": commands}, "requirements": list(ledger)}


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


async def _seed_run(
    db,
    workspace_id,
    rounds: list[tuple[VerificationOutcome, dict[str, Any]]],
    *,
    product_id: uuid.UUID | None = None,
    status=RunStatus.SHIPPED,
    age=0,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(hours=age)
    async with scoped_session(db, workspace_id) as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=status,
                payload={},
                created_at=base,
                updated_at=base,
            )
        )
        await s.flush()
        for i, (outcome, result) in enumerate(rounds):
            s.add(
                VerificationResult(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    outcome=outcome,
                    contract={},
                    result=result,
                    created_at=base + timedelta(minutes=i),
                )
            )
        await s.commit()
    return run_id


async def _mcp(db, registry, workspace_id, args=None):
    async with db() as s:
        return await registry.call_tool(TOOL, args or {}, _ctx(s, workspace_id))


async def _rest(db, workspace_id, query=""):
    import httpx

    from backend.api.deps import get_current_user, get_db_session, get_workspace_id
    from backend.api.main import create_app

    from .._support import fake_current_user, publishing_workspace

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
        resp = await c.get(f"/api/v1/runs/verification-quality{query}")
    assert resp.status_code == 200
    return resp.json()


async def _seed_mixed(db, workspace_id, mine, theirs):
    # A: command-failed round, ledger-only round, then a pass.
    await _seed_run(
        db,
        workspace_id,
        [
            (F, _result(_req("missing"), failed_command=True)),
            (F, _result(_req("missing"))),
            (
                P,
                _result(
                    _req("done", "holds"),
                    _req("unclear"),
                    _req("withdrawn"),
                    _req("done", "vacuous"),
                ),
            ),
        ],
        product_id=mine,
        age=0,
    )
    # B: a single ledger-only failure (rejected), run failed.
    await _seed_run(
        db,
        workspace_id,
        [(F, _result(_req("rejected"), _req("done", "holds")))],
        product_id=mine,
        status=RunStatus.FAILED,
        age=1,
    )
    # Never verified, and not finished: out of every denominator.
    await _seed_run(db, workspace_id, [], product_id=mine, age=2)
    await _seed_run(db, workspace_id, [(P, _result(_req("unclear")))], status=RunStatus.RUNNING)
    # Another product in the same workspace.
    await _seed_run(db, workspace_id, [(P, _result(_req("unclear")))], product_id=theirs, age=3)


async def test_five_figures_with_numerators_and_denominators_match_a_hand_count(
    db, workspace_id, registry, seeded
) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _seed_mixed(db, workspace_id, mine, theirs)

    out = await _mcp(db, registry, workspace_id, {"product_id": str(mine)})

    assert out["runs_considered"] == 3
    assert out["runs_verified"] == 2
    assert (out["verification_rounds"], out["avg_verification_rounds"]) == (4, 2.0)
    assert (out["ledger_unclear"], out["ledger_items"], out["ledger_unclear_rate"]) == (1, 5, 0.2)
    assert (
        out["counterfactual_vacuous"],
        out["counterfactual_checked"],
        out["counterfactual_vacuous_rate"],
    ) == (1, 3, 1 / 3)
    # command-failed round is not ledger-only; the other two failed rounds are
    assert (out["ledger_only_failures"], out["failed_rounds"]) == (2, 3)


async def test_a_round_with_a_failed_command_is_not_a_ledger_only_failure(
    db, workspace_id, registry, seeded
) -> None:
    await _seed_run(db, workspace_id, [(F, _result(_req("missing"), failed_command=True))])
    await _seed_run(
        db,
        workspace_id,
        [
            (
                F,
                {
                    "command_results": [{"command": "x", "passed": False}],
                    "requirements": [_req("missing")],
                },
            )
        ],
        age=1,
    )

    out = await _mcp(db, registry, workspace_id)

    assert (out["ledger_only_failures"], out["failed_rounds"]) == (0, 2)


async def test_no_verification_leaves_every_rate_null(db, workspace_id, registry, seeded) -> None:
    await _seed_run(db, workspace_id, [])

    out = await _mcp(db, registry, workspace_id)

    assert out["runs_verified"] == 0
    assert out["avg_verification_rounds"] is None
    assert out["ledger_unclear_rate"] is None
    assert out["counterfactual_vacuous_rate"] is None
    assert out["ledger_only_failures"] == 0


async def test_other_workspaces_and_products_are_not_counted(
    db, workspace_id, registry, seeded
) -> None:
    other = uuid.uuid4()
    async with scoped_session(db, other) as s:
        s.add(WorkspaceRow(id=other, name="other"))
        await s.commit()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _seed_run(db, workspace_id, [(P, _result(_req("done")))], product_id=mine)
    await _seed_run(db, workspace_id, [(F, _result(_req("missing")))], product_id=theirs, age=1)
    await _seed_run(db, other, [(F, _result(_req("missing")))], age=2)

    scoped = await _mcp(db, registry, workspace_id, {"product_id": str(mine)})
    whole = await _mcp(db, registry, workspace_id)

    assert (scoped["runs_verified"], scoped["ledger_only_failures"]) == (1, 0)
    assert (whole["runs_verified"], whole["ledger_only_failures"]) == (2, 1)


async def test_rest_and_mcp_give_the_same_values(db, workspace_id, registry, seeded) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _seed_mixed(db, workspace_id, mine, theirs)

    assert await _rest(db, workspace_id) == await _mcp(db, registry, workspace_id)
    assert await _rest(db, workspace_id, f"?product_id={mine}") == await _mcp(
        db, registry, workspace_id, {"product_id": str(mine)}
    )
