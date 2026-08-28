"""``bsvibe_knowledge_reindex_embeddings`` — the embedding backfill trigger on MCP.

The backfill itself (``reconcile_embeddings``) has deep coverage under
``tests/knowledge/retrieval``; what is proven here is that the founder can
actually REACH it. Before this tool the only deliberate trigger was
``POST /api/v1/inside/reindex-embeddings``, whose callers — measured across the
whole repo — were tests and nothing else: no MCP tool, no PWA control, no SDK
method. A backfill nobody can fire is a backfill that only runs by accident.

The region test is a positive/negative control pair, not a shape assertion: the
vault a workspace's notes actually live in is keyed by the workspace's OWN
``region`` (that is the boundary the settle hook writes through). Resolving it
from ``knowledge_default_region`` instead reads an empty directory and reports
``scanned: 0`` — a silent no-op wearing a success shape.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.knowledge.retrieval.db  # noqa: F401 — registers note_embeddings
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry, ToolScopeDenied
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio

TOOL = "bsvibe_knowledge_reindex_embeddings"


class _StubEmbedder:
    """Enabled embedder that never touches a provider."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str | None:
        return "stub-model"

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0, 0.0]


@pytest_asyncio.fixture
async def db() -> AsyncIterator:
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
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


def _write_note(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: T\n---\n\n# T\n\n{body}\n")


@pytest.fixture
def stub_embedder(monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    """Patch the tool module's embedder resolution + swap pgvector for memory."""
    embedder = _StubEmbedder()
    store = InMemoryNoteVectorBackend()
    monkeypatch.setattr(
        "backend.knowledge.retrieval.embedder_resolution.resolve_knowledge_embedder",
        lambda _settings: embedder,
    )
    monkeypatch.setattr(
        "backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend",
        lambda *a, **k: store,
    )
    embedder.store = store  # type: ignore[attr-defined]
    return embedder


@pytest_asyncio.fixture
async def seeded_ws(db, workspace_id, monkeypatch, tmp_path) -> AsyncIterator[Path]:
    """A workspace in a NON-default region, with its vault populated there."""
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_DEFAULT_REGION", "us-1")
    get_settings.cache_clear()
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="eu-9"))
        await s.commit()
    yield tmp_path
    get_settings.cache_clear()


async def test_reindex_tool_backfills_the_workspaces_knowledge_notes(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """The founder fires the backfill and gets the real counts back."""
    ws_vault = seeded_ws / "eu-9" / str(workspace_id)
    _write_note(ws_vault, "garden/seedling/a.md", "Alpha principle")
    _write_note(ws_vault, "concepts/active/c.md", "Gamma synthesis")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out == {"scanned": 2, "embedded": 2, "already": 0, "disabled": False}
    assert len(stub_embedder.calls) == 2


async def test_reindex_tool_reads_the_workspace_region_not_the_default(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """Negative control: notes under the DEFAULT region are not this workspace's.

    The workspace's region is ``eu-9``. A note parked under ``us-1`` (the
    deployment default) belongs to no one here — resolving the vault from the
    default region would scan it and report success over the wrong corpus.
    """
    _write_note(seeded_ws / "us-1" / str(workspace_id), "garden/seedling/x.md", "Wrong region")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out["scanned"] == 0, "scanned the default-region vault instead of the workspace's"
    assert stub_embedder.calls == []


async def test_reindex_tool_is_idempotent(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """Second pass re-embeds nothing — the fingerprint is unchanged."""
    ws_vault = seeded_ws / "eu-9" / str(workspace_id)
    _write_note(ws_vault, "garden/seedling/a.md", "Alpha principle")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        first = await registry.call_tool(TOOL, {}, ctx)
        second = await registry.call_tool(TOOL, {}, ctx)

    assert first["embedded"] == 1
    assert second == {"scanned": 1, "embedded": 0, "already": 1, "disabled": False}


async def test_reindex_tool_requires_write_scope(
    db, workspace_id, user_id, registry, seeded_ws
) -> None:
    """It mutates the index — ``mcp:read`` alone must not fire it."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolScopeDenied, match="requires scope"):
            await registry.call_tool(TOOL, {}, ctx)


async def test_reindex_tool_reports_disabled_when_no_embedding_model(
    db, workspace_id, user_id, registry, seeded_ws, monkeypatch
) -> None:
    """No deployment embedding model → ``disabled: true``, not a silent zero-scan.

    ``scanned: 0`` on its own is indistinguishable from "an empty corpus"; the
    flag is what tells the founder the backfill could not have run at all.
    """
    from backend.knowledge.retrieval.embedder_adapter import GatewayEmbedder

    monkeypatch.setattr(
        "backend.knowledge.retrieval.embedder_resolution.resolve_knowledge_embedder",
        lambda _settings: GatewayEmbedder(None),
    )
    _write_note(seeded_ws / "eu-9" / str(workspace_id), "garden/seedling/a.md", "Alpha")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out == {"scanned": 0, "embedded": 0, "already": 0, "disabled": True}
