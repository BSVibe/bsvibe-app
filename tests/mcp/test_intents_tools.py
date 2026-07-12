"""Intent authoring MCP tools — UI-parity surface (NL routing Lift N2).

Mirrors ``/api/v1/intents`` (see :mod:`backend.api.v1.intents`). Handlers
delegate to the SAME :mod:`backend.embedding.authoring` service the REST path
uses. The embedder is monkeypatched so no real embedding API is ever called.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.embedding.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
import backend.router.accounts.models  # noqa: F401
from backend.config import get_settings
from backend.embedding.service import EmbeddedExample
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


class _StubEmbedder:
    model = "stub/embed-1"

    async def embed_one(self, text: str) -> EmbeddedExample:
        return EmbeddedExample(text=text, embedding=[1.0, 0.0, 0.0], model=self.model)


@pytest_asyncio.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    import backend.mcp.tools.intents_tools as tools

    async def _stub_build(session, *, workspace_id, account_id):
        return _StubEmbedder()

    monkeypatch.setattr(tools, "build_account_embedder", _stub_build)


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


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_create_list_delete_round_trip(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        created = await registry.call_tool(
            "bsvibe_intents_create",
            {"name": "marketing", "threshold": 0.7, "examples": ["launch tweet", "blog post"]},
            ctx,
        )
    assert created["name"] == "marketing"
    assert created["threshold"] == 0.7
    intent_id = created["id"]

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_intents_list", {}, ctx)
    assert isinstance(listed, list)
    assert any(i["id"] == intent_id for i in listed)

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_intents_delete", {"intent_id": intent_id}, ctx)
    assert out["deleted"] is True


async def test_create_defaults_threshold(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        created = await registry.call_tool(
            "bsvibe_intents_create", {"name": "design", "examples": ["make a logo"]}, ctx
        )
    assert created["threshold"] == 0.65


async def test_create_duplicate_name_errors(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        await registry.call_tool("bsvibe_intents_create", {"name": "dup", "examples": []}, ctx)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="already exists"):
            await registry.call_tool("bsvibe_intents_create", {"name": "dup", "examples": []}, ctx)


async def test_create_without_embedding_config_is_graceful(
    db, workspace_id, user_id, registry, seeded, monkeypatch
) -> None:
    import backend.mcp.tools.intents_tools as tools

    async def _no_embedder(session, *, workspace_id, account_id):
        return None

    monkeypatch.setattr(tools, "build_account_embedder", _no_embedder)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        created = await registry.call_tool(
            "bsvibe_intents_create", {"name": "nomodel", "examples": ["some phrase"]}, ctx
        )
    assert created["name"] == "nomodel"


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_intents_create", {"name": "x", "examples": []}, ctx)


async def test_delete_unknown_errors(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool("bsvibe_intents_delete", {"intent_id": str(uuid.uuid4())}, ctx)


async def test_list_workspace_scoped(db, workspace_id, user_id, registry, seeded) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        await registry.call_tool("bsvibe_intents_create", {"name": "mine", "examples": []}, ctx)

    async with db() as s:
        ctx_other = ToolContext(
            principal=_principal(
                workspace_id=other_ws, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_intents_create", {"name": "theirs", "examples": []}, ctx_other
        )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_intents_list", {}, ctx)
    assert {i["name"] for i in listed} == {"mine"}
