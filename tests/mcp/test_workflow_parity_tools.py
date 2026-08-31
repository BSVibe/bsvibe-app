"""The MCP tools that close part of the REST parity gap — behaviour, not presence.

``test_rest_surface_has_an_mcp_twin`` proves a tool with the right name EXISTS.
That is not enough: a tool registered with a handler that returns ``{}`` would
satisfy it. These tests pin what each one actually does, and — for the mutation
— that the rule it runs is the *same* rule the REST route runs (the state change
is observed on the row, not on the response envelope).

Error-surface parity is asserted as a mapping, not as a message: REST answers
404/409, MCP answers ``ToolError``. The two surfaces are allowed to word it
differently; they are not allowed to disagree about which cases fail.

``retract`` and ``report`` are NOT here — closing those two needs the rule moved
out of ``backend.api`` first (the MCP import contract). They are pinned with
that reason in ``test_rest_surface_has_an_mcp_twin._KNOWN_GAPS``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws"))
        await s.commit()
        yield


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


async def _seed_run(db, workspace_id, *, status, payload=None) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=status,
                payload=payload or {},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return run_id


async def _seed_deliverable(
    db, workspace_id, run_id, *, payload=None, handles=None, retracted_at=None
) -> uuid.UUID:
    did = uuid.uuid4()
    async with db() as s:
        s.add(
            Deliverable(
                id=did,
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload=payload or {},
                compensation_handles=handles,
                retracted_at=retracted_at,
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return did


# ---------------------------------------------------------------------------
# bsvibe_runs_retry
# ---------------------------------------------------------------------------
async def test_runs_retry_reopens_a_failed_run(db, workspace_id, user_id, registry, seeded) -> None:
    """The state change, observed on the row — not on the response envelope."""
    run_id = await _seed_run(db, workspace_id, status=RunStatus.FAILED)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_runs_retry", {"run_id": str(run_id)}, ctx)
    assert out["status"] == "open"
    assert out["retry_count"] == 1
    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.OPEN
        # The retry marker + restarted clock the REST route writes must be
        # written here too — the review surfaces count elapsed time from it.
        assert run.payload["retry_count"] == 1
        assert run.payload["restarted_at"]


async def test_runs_retry_is_refused_for_a_non_terminal_run(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """REST answers 409 here; MCP must refuse too, not silently re-open."""
    run_id = await _seed_run(db, workspace_id, status=RunStatus.RUNNING)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="running|failed or cancelled"):
            await registry.call_tool("bsvibe_runs_retry", {"run_id": str(run_id)}, ctx)
    async with db() as s:
        assert (await s.get(ExecutionRun, run_id)).status is RunStatus.RUNNING


async def test_runs_retry_never_crosses_the_workspace_boundary(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Another workspace's failed run must read as not-found, never as retryable."""
    other = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other, name="other"))
        await s.commit()
    run_id = await _seed_run(db, other, status=RunStatus.FAILED)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool("bsvibe_runs_retry", {"run_id": str(run_id)}, ctx)
    async with db() as s:
        assert (await s.get(ExecutionRun, run_id)).status is RunStatus.FAILED


# ---------------------------------------------------------------------------
# bsvibe_deliverables_diff
# ---------------------------------------------------------------------------
async def test_deliverables_diff_serves_the_captured_patch(
    db, workspace_id, user_id, registry, seeded
) -> None:
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    did = await _seed_deliverable(db, workspace_id, run_id, payload={"diff": patch})
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_diff", {"deliverable_id": str(did)}, ctx
        )
    assert out["diff"] == patch
    assert out["truncated"] is False


async def test_deliverables_diff_is_calm_when_no_diff_was_captured(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A Direct run captures no diff. REST returns ``diff: null``, not 404 — same here."""
    run_id = await _seed_run(db, workspace_id, status=RunStatus.SHIPPED)
    did = await _seed_deliverable(db, workspace_id, run_id, payload={})
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_deliverables_diff", {"deliverable_id": str(did)}, ctx
        )
    assert out["diff"] is None


# ---------------------------------------------------------------------------
# Scope surface — a read token must not be able to mutate.
# ---------------------------------------------------------------------------
async def test_retry_requires_a_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    """A read token may look at runs; it may not re-open one."""
    tool, args_key = "bsvibe_runs_retry", "run_id"
    # An unknown tool name also raises ToolError, which would make the assertion
    # below pass while the tool does not exist at all. Pin existence first.
    assert registry.get(tool) is not None
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError):
            await registry.call_tool(tool, {args_key: str(uuid.uuid4())}, ctx)
