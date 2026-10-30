"""Big-change visibility — the report and the Safe Mode queue say how many files change.

Real ``Deliverable`` rows go in; the REAL MCP handlers (``bsvibe_deliverables_report`` and
``bsvibe_safe_mode_list_pending``) read them back. Nothing here builds a response by hand.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.delivery.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.change_footprint import footprint_of
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine
from ._rls import scoped_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]

BIG_DIR = ".pnpm-store"


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


def _ctx(session: Any, workspace_id: uuid.UUID, user_id: uuid.UUID) -> ToolContext:
    principal = McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset({"mcp:read"}),
        jti=uuid.uuid4(),
    )
    return ToolContext(principal=principal, session=session)


async def _store(db, workspace_id: uuid.UUID, user_id: uuid.UUID, payload: dict) -> uuid.UUID:
    """Persist workspace + run + a Deliverable with ``payload`` + a pending queue item."""
    async with scoped_session(db, workspace_id) as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws"))
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
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
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.DIRECT_OUTPUT,
            payload=payload,
        )
        s.add(deliverable)
        await s.flush()
        await SafeModeQueue(s).enqueue(
            workspace_id=workspace_id, deliverable_id=deliverable.id, run_id=run.id
        )
        await s.commit()
        return deliverable.id


async def _read(db, registry, workspace_id, user_id, deliverable_id):
    async with db() as s:
        ctx = _ctx(s, workspace_id, user_id)
        report = await registry.call_tool(
            "bsvibe_deliverables_report", {"deliverable_id": str(deliverable_id)}, ctx
        )
        pending = await registry.call_tool("bsvibe_safe_mode_list_pending", {}, ctx)
    return report, pending["items"][0]


async def test_large_change_is_reported_on_report_and_pending_item(db, registry) -> None:
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    refs = [f"{BIG_DIR}/v3/files/{i:03}" for i in range(240)] + [
        f"src/mod{i}.py" for i in range(10)
    ]
    deliverable_id = await _store(db, workspace_id, user_id, {"artifact_refs": refs})

    report, item = await _read(db, registry, workspace_id, user_id, deliverable_id)

    for view in (report, item):
        assert view["changed_files"] == 250
        assert view["large_change"] is True
        assert view["top_dirs"][0] == {"dir": BIG_DIR, "count": 240}
        assert view["top_dirs"][1] == {"dir": "src", "count": 10}
        assert view["large_change_warning"]
        assert BIG_DIR in view["large_change_warning"]


async def test_small_change_has_no_warning(db, registry) -> None:
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    refs = ["src/a.py", "src/b.py", "README.md"]
    deliverable_id = await _store(db, workspace_id, user_id, {"artifact_refs": refs})

    report, item = await _read(db, registry, workspace_id, user_id, deliverable_id)

    for view in (report, item):
        assert view["changed_files"] == 3
        assert view["large_change"] is False
        assert not view["large_change_warning"]
        assert view["top_dirs"][0] == {"dir": "src", "count": 2}


@pytest.mark.parametrize("payload", [{}, {"artifact_refs": []}, {"artifact_refs": "oops"}])
async def test_deliverable_without_artifact_refs_does_not_break(db, registry, payload) -> None:
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    deliverable_id = await _store(db, workspace_id, user_id, payload)

    report, item = await _read(db, registry, workspace_id, user_id, deliverable_id)

    for view in (report, item):
        assert view["changed_files"] is None
        assert view["large_change"] is False
        assert not view["large_change_warning"]
        assert view["top_dirs"] == []


async def test_threshold_is_exclusive_and_top_dirs_capped_at_five() -> None:
    refs = [f"d{i}/f" for i in range(7)]
    assert footprint_of(refs, threshold=7).large_change is False
    assert footprint_of(refs, threshold=6).large_change is True
    assert len(footprint_of(refs, threshold=6).top_dirs) == 5
    assert footprint_of(["README.md"], threshold=1).top_dirs[0].dir == "."
