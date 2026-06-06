"""Knowledge retract / correct / undo tool handler tests — Lift D3c."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.knowledge.infrastructure.ontology_db  # noqa: F401
from backend.config import get_settings
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_REGION = "us-1"

_NOTE_TEMPLATE = (
    "---\n"
    "kind: decision_resolution\n"
    "question: Should we cache the homepage?\n"
    "answer: Yes — 5 minute CDN TTL.\n"
    "captured_at: '2026-06-01T00:00:00Z'\n"
    "tags:\n"
    "  - settle\n"
    "  - decision\n"
    "---\n"
    "# Decision\n"
    "Cache the homepage at the CDN.\n"
)


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch) -> AsyncIterator:
    """Real fs sandbox under ``tmp_path/vault`` + reachable async session."""
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_DEFAULT_REGION", _REGION)
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


@pytest.fixture
def seeded_vault(tmp_path: Path, workspace_id: uuid.UUID) -> str:
    rel_path = "garden/seedling/cache-homepage.md"
    note_path = tmp_path / "vault" / _REGION / str(workspace_id) / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(_NOTE_TEMPLATE, encoding="utf-8")
    return rel_path


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


async def test_retract_issues_signal(db, workspace_id, user_id, registry, seeded_vault) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_retract",
            {"node_ref": seeded_vault, "reason": "wrong answer"},
            ctx,
        )
    assert out["created"] is True
    assert out["undo_window_seconds"] == 30
    assert out["signal"]["action"] == "retract"
    assert out["signal"]["node_ref"] == seeded_vault
    assert out["signal"]["reason"] == "wrong answer"


async def test_retract_idempotent_on_correction_id(
    db, workspace_id, user_id, registry, seeded_vault
) -> None:
    correction_id = str(uuid.uuid4())
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        first = await registry.call_tool(
            "bsvibe_knowledge_retract",
            {"node_ref": seeded_vault, "correction_id": correction_id},
            ctx,
        )
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        second = await registry.call_tool(
            "bsvibe_knowledge_retract",
            {"node_ref": seeded_vault, "correction_id": correction_id},
            ctx,
        )
    assert first["created"] is True
    assert second["created"] is False
    assert first["signal"]["id"] == second["signal"]["id"]


async def test_retract_404_on_missing_node(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="node not found"):
            await registry.call_tool(
                "bsvibe_knowledge_retract",
                {"node_ref": "garden/ghost.md"},
                ctx,
            )


async def test_correct_issues_signal(db, workspace_id, user_id, registry, seeded_vault) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_correct",
            {
                "node_ref": seeded_vault,
                "corrections": {"body": "New body text."},
                "reason": "missed nuance",
            },
            ctx,
        )
    assert out["signal"]["action"] == "correct"
    assert out["created"] is True


async def test_undo_returns_undone_inside_window(
    db, workspace_id, user_id, registry, seeded_vault
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        issued = await registry.call_tool(
            "bsvibe_knowledge_retract",
            {"node_ref": seeded_vault},
            ctx,
        )
        correction_id = issued["signal"]["id"]
        undone = await registry.call_tool(
            "bsvibe_knowledge_undo_correction",
            {"correction_id": correction_id},
            ctx,
        )
    assert undone["status"] == "undone"
    assert undone["correction_id"] == correction_id


async def test_undo_404_on_missing(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="correction not found"):
            await registry.call_tool(
                "bsvibe_knowledge_undo_correction",
                {"correction_id": str(uuid.uuid4())},
                ctx,
            )


async def test_retract_requires_write_scope(
    db, workspace_id, user_id, registry, seeded_vault
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_knowledge_retract",
                {"node_ref": seeded_vault},
                ctx,
            )


async def test_correct_requires_write_scope(
    db, workspace_id, user_id, registry, seeded_vault
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_knowledge_correct",
                {"node_ref": seeded_vault, "corrections": {"body": "x"}},
                ctx,
            )


async def test_undo_requires_write_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_knowledge_undo_correction",
                {"correction_id": str(uuid.uuid4())},
                ctx,
            )


async def test_retract_traversal_rejected(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="invalid node_ref|node not found"):
            await registry.call_tool(
                "bsvibe_knowledge_retract",
                {"node_ref": "../escape.md"},
                ctx,
            )
