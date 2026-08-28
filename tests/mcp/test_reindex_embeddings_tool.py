"""``bsvibe_knowledge_reindex_embeddings`` — the embedding backfill trigger on MCP.

The backfill itself (``reconcile_embeddings``) has deep coverage under
``tests/knowledge/retrieval``; what is proven here is that the founder can
actually REACH it. Before this tool the only deliberate trigger was
``POST /api/v1/inside/reindex-embeddings``, whose callers — measured across the
whole repo — were tests and nothing else: no MCP tool, no PWA control, no SDK
method. A backfill nobody can fire is a backfill that only runs by accident.

The region test is a negative control, not a shape assertion. The vault a
workspace's notes live in is keyed by the DEPLOYMENT region — one directory for
the whole install (see ``backend.knowledge.graph.vault_paths``). The workspaces
row still carries a stale ``region`` column; a reader that trusted it would open
an empty directory and report ``scanned: 0`` — a silent no-op wearing a success
shape. The fixture therefore seeds a row whose column DISAGREES with the
deployment, so a regression to reading it fails loudly.
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
    """A workspace whose stored ``region`` disagrees with the deployment's.

    The vault is under ``us-1`` (the deployment region, where every writer
    actually puts notes); the row says ``eu-9``. Any reader that resolved the
    vault from the row would find nothing.
    """
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
    ws_vault = seeded_ws / "us-1" / str(workspace_id)
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

    assert out == {
        "scanned": 2,
        "embedded": 2,
        "already": 0,
        "disabled": False,
        "remaining": 0,
        "removed": 0,
    }
    assert len(stub_embedder.calls) == 2


async def test_reindex_tool_ignores_the_stale_region_column(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """Negative control: the stored ``region`` must not steer the vault lookup.

    The row says ``eu-9``; the deployment writes to ``us-1``. A note parked
    under the row's region belongs to no vault anyone writes — a reader that
    trusted the column would scan it and report success over a corpus the
    settle hook never fills.
    """
    _write_note(seeded_ws / "eu-9" / str(workspace_id), "garden/seedling/x.md", "Stale region")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out["scanned"] == 0, "resolved the vault from the workspaces.region column"
    assert stub_embedder.calls == []


async def test_reindex_tool_scans_only_this_workspaces_vault(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """Negative control: the workspace id is the boundary that survives.

    With region collapsed to a deployment constant, every workspace's vault is
    a sibling directory under the same region — so the id is the ONLY thing
    keeping one founder's corpus out of another's backfill.
    """
    other = uuid.uuid4()
    _write_note(seeded_ws / "us-1" / str(other), "garden/seedling/x.md", "Someone else's note")
    _write_note(seeded_ws / "us-1" / str(workspace_id), "garden/seedling/a.md", "Mine")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out["scanned"] == 1, "scanned a neighbouring workspace's vault"
    assert len(stub_embedder.calls) == 1
    assert "Mine" in stub_embedder.calls[0]
    assert "Someone else's note" not in stub_embedder.calls[0]


async def test_reindex_tool_is_idempotent(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder
) -> None:
    """Second pass re-embeds nothing — the fingerprint is unchanged."""
    ws_vault = seeded_ws / "us-1" / str(workspace_id)
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
    assert second == {
        "scanned": 1,
        "embedded": 0,
        "already": 1,
        "disabled": False,
        "remaining": 0,
        "removed": 0,
    }


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
    _write_note(seeded_ws / "us-1" / str(workspace_id), "garden/seedling/a.md", "Alpha")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(TOOL, {}, ctx)

    assert out == {
        "scanned": 0,
        "embedded": 0,
        "already": 0,
        "disabled": True,
        "remaining": 0,
        "removed": 0,
    }


async def test_reindex_tool_bounds_one_pass_and_reports_the_rest(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder, monkeypatch
) -> None:
    """An HTTP-shaped caller must answer before its proxy gives up.

    Measured on prod 2026-08-28: a 1,685-note pass ran ~12 minutes, committed
    once at the end, and Cloudflare cut the client at 125s — so the surface
    reported failure for work that had succeeded. The pass is now bounded and
    reports ``remaining``; the caller re-invokes until it is 0.
    """
    monkeypatch.setattr("backend.mcp.tools.reindex_tools._HTTP_PASS_MAX_EMBEDS", 2)
    ws_vault = seeded_ws / "us-1" / str(workspace_id)
    for i in range(5):
        _write_note(ws_vault, f"garden/seedling/n{i}.md", f"Body {i}")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        first = await registry.call_tool(TOOL, {}, ctx)

    assert first["embedded"] == 2
    assert first["remaining"] == 3


async def test_reindex_tool_drains_across_calls_to_zero_remaining(
    db, workspace_id, user_id, registry, seeded_ws, stub_embedder, monkeypatch
) -> None:
    """Repeated calls finish the corpus — the resume protocol, with no job row."""
    monkeypatch.setattr("backend.mcp.tools.reindex_tools._HTTP_PASS_MAX_EMBEDS", 2)
    ws_vault = seeded_ws / "us-1" / str(workspace_id)
    for i in range(5):
        _write_note(ws_vault, f"garden/seedling/n{i}.md", f"Body {i}")

    total = 0
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        for _ in range(10):
            out = await registry.call_tool(TOOL, {}, ctx)
            total += out["embedded"]
            if out["remaining"] == 0:
                break

    assert total == 5
    assert out["remaining"] == 0
