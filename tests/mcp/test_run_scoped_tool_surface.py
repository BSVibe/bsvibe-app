"""What a run-scoped task token may SEE and CALL over MCP.

A dispatched executor task carries a token bound to one run and hands it to a CLI subprocess
on the founder's machine, for 90 minutes. Two things follow, and neither was true before this
module existed (both measured against prod, 2026-07-14):

* **Blast radius.** The token could call every workspace-wide tool — ``bsvibe_products_list``
  succeeded with it, and so would ``safe_mode_set`` / ``workers_revoke`` / ``knowledge_correct``.
  The design's own rule is that it "must not be a workspace-wide credential". So a principal
  that names a run sees the work tools and nothing else.

* **The agent's tool surface must be exactly what we sanctioned.** The worker hands the CLI
  ``--allowedTools <the 9 work tools>`` and then verifies the CLI's own ``system/init`` against
  that list. While the server offered all 86 tools, the CLI exposed all 86, and the check
  ``exposed - allowed`` flagged the other 77 — i.e. the agentic run could never start.

The discriminator is the ``bsvibe_work_`` prefix, which every work tool already carries.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.config import get_settings
from backend.dispatch.adapter import WORK_TOOL_NAMES
from backend.mcp.api import McpPrincipal
from backend.mcp.principal import reset_request_principal, set_request_principal
from backend.mcp.server import build_registry, build_server
from backend.mcp.tools.work_tools import WORK_TOOL_PREFIX

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


async def _unused_effect(*a, **k):  # pragma: no cover — the work tools are never CALLED here
    raise AssertionError("not reached")


def _server(db):
    """The production shape: the work tools are registered only when the composition root
    injects the two loop-owned effects."""
    registry = build_registry(record_question=_unused_effect, record_deliverable=_unused_effect)
    return build_server(session_factory=db, registry=registry)


def _principal(*, run_id: uuid.UUID | None) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


async def _list_names(server, principal: McpPrincipal) -> list[str]:
    token = set_request_principal(principal)
    try:
        result = await server.request_handlers[ListToolsRequest](
            ListToolsRequest(method="tools/list")
        )
        return [t.name for t in result.root.tools]
    finally:
        reset_request_principal(token)


async def test_a_run_scoped_token_sees_only_the_work_tools(db) -> None:
    """86 tools offered to a task token is both a blast radius and a broken init check."""
    server = _server(db)

    names = await _list_names(server, _principal(run_id=uuid.uuid4()))

    assert names, "a run-scoped principal must still see its work tools"
    assert all(n.startswith(WORK_TOOL_PREFIX) for n in names), (
        f"a run-scoped token must see ONLY work tools; got non-work: "
        f"{[n for n in names if not n.startswith(WORK_TOOL_PREFIX)]}"
    )
    assert set(names) == set(WORK_TOOL_NAMES), (
        "the surface the CLI is given (--allowedTools) and the surface the server offers must "
        "be the same set, or the worker's system/init check aborts every agentic run"
    )


async def test_an_ordinary_token_still_sees_the_whole_surface(db) -> None:
    """The founder's own MCP client is unchanged — this narrows the TASK token only."""
    server = _server(db)

    names = await _list_names(server, _principal(run_id=None))

    assert "bsvibe_products_list" in names
    assert "bsvibe_safe_mode_approve" in names
    assert len(names) > len(WORK_TOOL_NAMES)


async def test_a_run_scoped_token_cannot_call_a_workspace_wide_tool(db) -> None:
    """Hiding a tool from tools/list is cosmetic if the call still lands — gate the call too.

    Measured against prod: `bsvibe_products_list` returned the workspace's products to a
    run-scoped task token.
    """
    server = _server(db)
    principal = _principal(run_id=uuid.uuid4())
    token = set_request_principal(principal)
    try:
        result = await server.request_handlers[CallToolRequest](
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="bsvibe_products_list", arguments={}),
            )
        )
    finally:
        reset_request_principal(token)

    # The SDK turns a raised ToolError into an error result rather than propagating it — that
    # error frame is what the agent's CLI actually sees.
    payload = result.root
    assert payload.isError is True, "a task token must not reach a workspace-wide tool"
    body = " ".join(c.text for c in payload.content if hasattr(c, "text")).lower()
    assert "work tool" in body or "run" in body


async def test_every_sanctioned_name_is_a_work_tool_name() -> None:
    """The CLI allowlist and the server's work-tool prefix must not drift apart."""
    assert all(n.startswith(WORK_TOOL_PREFIX) for n in WORK_TOOL_NAMES)
