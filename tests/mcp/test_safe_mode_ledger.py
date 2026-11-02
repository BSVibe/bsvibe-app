"""A Safe Mode queue item carries its run's requirements-ledger verdict.

Real run + ``VerificationResult`` + queue rows, read through the real MCP tool and REST
routes — and compared with the deliverable report's own ``ledger_counts``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.delivery.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.safe_mode_ledger import ledgers_for_runs
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user, publishing_workspace
from ._rls import scoped_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]


def _row(rid: str, status: str, text: str | None = None) -> dict[str, Any]:
    return {
        "id": rid,
        "text": text or f"item {rid}",
        "kind": "do",
        "status": status,
        "paths": [],
        "tests": [],
        "note": "",
    }


MIXED = [_row("R1", "unclear"), _row("R2", "done"), _row("R3", "deferred"), _row("R4", "done")]


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def client(db, workspace_id):
    app = create_app()
    user = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = lambda: user
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def _hold(db, workspace_id: uuid.UUID, ledger: list[dict[str, Any]] | None) -> uuid.UUID:
    """A run (with ``ledger`` on a stored VerificationResult, if any) holding one queue item."""
    async with scoped_session(db, workspace_id) as s:
        if await s.get(WorkspaceRow, workspace_id) is None:
            s.add(WorkspaceRow(id=workspace_id, name="ws"))
            await s.flush()
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(run)
        await s.flush()
        if ledger is not None:
            s.add(
                VerificationResult(
                    run_id=run.id,
                    workspace_id=workspace_id,
                    outcome=VerificationOutcome.PASSED,
                    contract={},
                    result={"requirements": ledger},
                )
            )
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.DIRECT_OUTPUT,
            payload={},
        )
        s.add(deliverable)
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable.id, run_id=run.id
        )
        await s.commit()
        return deliverable.id


def _ctx(session: Any, workspace_id: uuid.UUID) -> ToolContext:
    principal = McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset({"mcp:read"}),
        jti=uuid.uuid4(),
    )
    return ToolContext(principal=principal, session=session)


async def _mcp(db, registry, workspace_id, deliverable_id):
    async with db() as s:
        ctx = _ctx(s, workspace_id)
        report = await registry.call_tool(
            "bsvibe_deliverables_report", {"deliverable_id": str(deliverable_id)}, ctx
        )
        pending = await registry.call_tool("bsvibe_safe_mode_list_pending", {}, ctx)
    item = next(i for i in pending["items"] if i["deliverable_id"] == str(deliverable_id))
    return report, item


async def _rest(client, deliverable_id):
    flat = (await client.get("/api/v1/safemode/queue")).json()
    grouped = (await client.get("/api/v1/safemode/queue/by-run")).json()
    pick = lambda items: next(i for i in items if i["deliverable_id"] == str(deliverable_id))  # noqa: E731
    return pick(flat), pick([i for g in grouped for i in g["items"]])


async def test_rest_and_mcp_carry_the_report_ledger_counts(db, registry, client, workspace_id):
    deliverable_id = await _hold(db, workspace_id, MIXED)

    report, mcp_item = await _mcp(db, registry, workspace_id, deliverable_id)
    rest_flat, rest_grouped = await _rest(client, deliverable_id)

    assert report["ledger_counts"] == {
        "requirements_total": 4,
        "requirements_done": 2,
        "requirements_deferred": 1,
        "requirements_unclear": 1,
    }
    for item in (mcp_item, rest_flat, rest_grouped):
        assert item["ledger_counts"] == report["ledger_counts"]


async def test_only_unclear_and_deferred_are_attention(db, registry, client, workspace_id):
    deliverable_id = await _hold(db, workspace_id, MIXED)

    _, mcp_item = await _mcp(db, registry, workspace_id, deliverable_id)
    rest_flat, rest_grouped = await _rest(client, deliverable_id)

    expected = [{"id": "R1", "text": "item R1"}, {"id": "R3", "text": "item R3"}]
    for item in (mcp_item, rest_flat, rest_grouped):
        assert item["ledger_attention"] == expected


async def test_run_without_ledger_is_empty(db, registry, client, workspace_id):
    no_verification = await _hold(db, workspace_id, None)
    no_ledger = await _hold(db, workspace_id, [])

    for deliverable_id in (no_verification, no_ledger):
        _, mcp_item = await _mcp(db, registry, workspace_id, deliverable_id)
        rest_flat, rest_grouped = await _rest(client, deliverable_id)
        for item in (mcp_item, rest_flat, rest_grouped):
            assert item["ledger_counts"] == {}
            assert item["ledger_attention"] == []


async def test_ledgers_are_read_in_one_query(db, workspace_id):
    deliverables = [await _hold(db, workspace_id, MIXED) for _ in range(3)]
    assert len(deliverables) == 3
    async with db() as s:
        run_ids = [i.run_id for i in await SafeModeQueue(s).list_pending(workspace_id=workspace_id)]
        seen: list[str] = []

        def _record(conn, cursor, statement, *_args):
            seen.append(statement)

        engine = s.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            ledgers = await ledgers_for_runs(s, workspace_id=workspace_id, run_ids=run_ids)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    assert len(ledgers) == 3
    assert len([q for q in seen if "verification_results" in q]) == 1
