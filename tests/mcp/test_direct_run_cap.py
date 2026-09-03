"""The concurrent-run cap holds on the MCP surface too.

``bsvibe_direct`` is the same founder submission as ``POST /api/v1/messages``.
A cap enforced only at the REST door would be bypassed by every agent client,
so the rule lives in :mod:`backend.workflow.application.run_caps` and both
doors consult it — each raising its own kind of error (mirrored surfaces drift
in the direction of least testing; the RULE is shared, the error face is not).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

import backend.identity.db  # noqa: F401 — register mappers
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

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


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID) -> McpPrincipal:
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(("mcp:read", "mcp:write")),
        jti=uuid.uuid4(),
    )


async def _seed(db, *, workspace_id: uuid.UUID, user_id: uuid.UUID, cap: int | None, held: int):
    now = datetime.now(tz=UTC)
    product_id = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", max_concurrent_runs=cap))
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                created_at=now,
                updated_at=now,
            )
        )
        for _ in range(held):
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    product_id=product_id,
                    status=RunStatus.REVIEW_READY,
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
        await s.commit()


async def test_mcp_direct_is_refused_at_the_cap(db, registry, workspace_id, user_id) -> None:
    await _seed(db, workspace_id=workspace_id, user_id=user_id, cap=3, held=3)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id), session=s
        )
        with pytest.raises(ToolError, match="concurrent"):
            await registry.call_tool("bsvibe_direct", {"text": "one too many"}, ctx)


async def test_mcp_direct_is_accepted_below_the_cap(db, registry, workspace_id, user_id) -> None:
    """The control — without it the refusal above is also satisfied by a tool
    that refuses everything (or by ``bsvibe_direct`` not existing at all)."""
    await _seed(db, workspace_id=workspace_id, user_id=user_id, cap=3, held=2)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id), session=s
        )
        result = await registry.call_tool("bsvibe_direct", {"text": "still room"}, ctx)

    assert result["accepted"] is True
