"""Drive the MCP ``Server`` via its internal request_handlers — Lift D2.

The MCP SDK exposes a ``server.request_handlers`` registry keyed by the
typed Request classes; tests can hand them a request object and observe
the wrapped response without spinning up the Streamable HTTP transport.
This pattern keeps the server-side dispatch path covered without bringing
``anyio`` task groups + ``StreamableHTTPSessionManager`` into the test
loop (which has known cross-task cancel-scope pitfalls under
pytest-asyncio).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    ListToolsRequest,
)

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal
from backend.mcp.principal import (
    reset_request_principal,
    set_request_principal,
)
from backend.mcp.server import build_server

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


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded(db, workspace_id, user_id) -> AsyncIterator[None]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="t", email="t@e.co"))
        await s.flush()
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="alpha"))
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


async def test_server_list_tools_returns_every_registered_tool(db) -> None:
    server = build_server(session_factory=db)
    handler = server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    payload = result.root
    names = [t.name for t in payload.tools]
    assert "bsvibe_products_list" in names
    assert "bsvibe_safe_mode_approve" in names
    assert "bsvibe_direct" in names
    assert "bsvibe_knowledge_search" in names


async def test_server_call_tool_returns_text_content_with_json_body(
    db, workspace_id, user_id, seeded
) -> None:
    server = build_server(session_factory=db)
    handler = server.request_handlers[CallToolRequest]
    principal = _principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",))
    token = set_request_principal(principal)
    try:
        result = await handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="bsvibe_products_list",
                    arguments={},
                ),
            )
        )
    finally:
        reset_request_principal(token)
    payload = result.root
    # The MCP SDK wraps successful tool calls in a CallToolResult — its
    # ``content`` is a list of TextContent (our server writes one JSON blob).
    text = payload.content[0].text
    body = json.loads(text)
    assert isinstance(body, list)
    assert any(p["slug"] == "alpha" for p in body)
