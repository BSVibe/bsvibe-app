"""Knowledge tool handler tests — vault-direct read+seed surface."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

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
async def seeded(db, workspace_id) -> AsyncIterator[Path]:
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
    settings = get_settings()
    vault = Path(settings.knowledge_vault_root) / "us-1" / str(workspace_id)
    (vault / "garden").mkdir(parents=True, exist_ok=True)
    (vault / "garden" / "alpha.md").write_text(
        "# Alpha\n\n#topic-a #shared\n\nAlpha body content.\n", encoding="utf-8"
    )
    (vault / "garden" / "beta.md").write_text(
        "# Beta\n\n#topic-b #shared\n\nBeta has something special inside.\n",
        encoding="utf-8",
    )
    yield vault


async def test_list_recent_returns_excerpts_and_tags(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_recent", {}, ctx)
    assert out["total"] == 2
    paths = {n["path"] for n in out["notes"]}
    assert paths == {"garden/alpha.md", "garden/beta.md"}
    all_tags = {tag for n in out["notes"] for tag in n["tags"]}
    assert "shared" in all_tags


async def test_get_note_returns_content(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_get_note", {"path": "garden/alpha.md"}, ctx
        )
    assert out["path"] == "garden/alpha.md"
    assert "Alpha body" in out["content"]
    assert "topic-a" in out["tags"]


async def test_get_note_missing_raises(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool("bsvibe_knowledge_get_note", {"path": "garden/nope.md"}, ctx)


async def test_search_finds_substring(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_search", {"query": "something special"}, ctx
        )
    assert out["total"] == 1
    assert out["results"][0]["path"] == "garden/beta.md"


async def test_list_tags_aggregates_counts(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_tags", {}, ctx)
    by_tag = {t["tag"]: t["count"] for t in out["tags"]}
    assert by_tag["shared"] == 2
    assert by_tag["topic-a"] == 1


async def test_create_note_writes_seed_and_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_create_note",
            {"title": "MCP Seed", "content": "body", "tags": ["mcp"]},
            ctx,
        )
    assert out["seed_path"].startswith("seeds/mcp/")
    assert out["bytes_written"] > 0
    # And on disk:
    seeded_path = seeded / out["seed_path"]
    assert seeded_path.exists()
    body = seeded_path.read_text(encoding="utf-8")
    assert "MCP Seed" in body
    assert "#mcp" in body


async def test_create_note_denies_without_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_knowledge_create_note", {"title": "X"}, ctx)
