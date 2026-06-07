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


# ---------------------------------------------------------------------------
# Lift E10 — recursive subdir walk for list_recent / search / list_tags.
#
# The pre-E10 tools only walked one directory level, so a founder running
# `list_recent({subdir: "garden"})` after IngestCompiler dropped 46+ notes
# under `garden/entities/*.md` would see `total: 0`. These tests pin the
# recursive=True default and the opt-out flag.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_nested(db, workspace_id) -> AsyncIterator[Path]:
    """Vault layout matching the IngestCompiler dogfood output:

    - garden/top.md                   (direct child)
    - garden/entities/foo.md          (subdir)
    - garden/seedling/bar.md          (subdir)
    - garden/entities/deep/baz.md     (sub-subdir, confirms full subtree walk)
    - concepts/active/qux.md          (root-level recursion)
    """
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
    settings = get_settings()
    vault = Path(settings.knowledge_vault_root) / "us-1" / str(workspace_id)
    (vault / "garden").mkdir(parents=True, exist_ok=True)
    (vault / "garden" / "entities").mkdir(parents=True, exist_ok=True)
    (vault / "garden" / "entities" / "deep").mkdir(parents=True, exist_ok=True)
    (vault / "garden" / "seedling").mkdir(parents=True, exist_ok=True)
    (vault / "concepts" / "active").mkdir(parents=True, exist_ok=True)

    # Write in mtime order — oldest first, freshest last. mtime sort desc
    # MUST put baz/qux ahead of top.
    import os
    import time

    (vault / "garden" / "top.md").write_text(
        "# Top\n\n#root\n\nTop-level garden note.\n", encoding="utf-8"
    )
    os.utime(vault / "garden" / "top.md", (1000, 1000))

    (vault / "garden" / "entities" / "foo.md").write_text(
        "# Foo\n\n#entity\n\nFoo entity body — searchable widget.\n",
        encoding="utf-8",
    )
    os.utime(vault / "garden" / "entities" / "foo.md", (2000, 2000))

    (vault / "garden" / "seedling" / "bar.md").write_text(
        "# Bar\n\n#seedling\n\nBar seedling body.\n", encoding="utf-8"
    )
    os.utime(vault / "garden" / "seedling" / "bar.md", (3000, 3000))

    (vault / "garden" / "entities" / "deep" / "baz.md").write_text(
        "# Baz\n\n#deep\n\nBaz at depth 3.\n", encoding="utf-8"
    )
    os.utime(vault / "garden" / "entities" / "deep" / "baz.md", (4000, 4000))

    (vault / "concepts" / "active" / "qux.md").write_text(
        "# Qux\n\n#concept\n\nQux active concept.\n", encoding="utf-8"
    )
    os.utime(vault / "concepts" / "active" / "qux.md", (5000, 5000))

    # Avoid unused-import noise from `time`
    _ = time
    yield vault


async def test_list_recent_recurses_into_subdirs_by_default(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """`list_recent({subdir: "garden"})` MUST walk the entire garden subtree."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_recent", {"subdir": "garden"}, ctx)
    paths = {n["path"] for n in out["notes"]}
    assert paths == {
        "garden/top.md",
        "garden/entities/foo.md",
        "garden/seedling/bar.md",
        "garden/entities/deep/baz.md",
    }
    assert out["total"] == 4


async def test_list_recent_sorts_by_mtime_desc(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """Most-recently-modified first — founder UX is 'what just happened?'."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_recent", {"subdir": "garden"}, ctx)
    paths = [n["path"] for n in out["notes"]]
    # baz (mtime=4000) > bar (3000) > foo (2000) > top (1000)
    assert paths == [
        "garden/entities/deep/baz.md",
        "garden/seedling/bar.md",
        "garden/entities/foo.md",
        "garden/top.md",
    ]


async def test_list_recent_recursive_false_only_returns_direct_children(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_list_recent",
            {"subdir": "garden", "recursive": False},
            ctx,
        )
    paths = {n["path"] for n in out["notes"]}
    assert paths == {"garden/top.md"}
    assert out["total"] == 1


async def test_list_recent_empty_subdir_walks_whole_vault(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """Founder-friendly 'what's in my knowledge graph right now?' — empty subdir
    + recursive=True (default) → walk EVERYTHING under the workspace vault."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_recent", {"subdir": ""}, ctx)
    paths = {n["path"] for n in out["notes"]}
    # All 5 notes, including the concepts/active one
    assert "concepts/active/qux.md" in paths
    assert "garden/entities/deep/baz.md" in paths
    assert out["total"] == 5


async def test_list_recent_limit_applied_after_mtime_sort(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """`limit` MUST keep the freshest notes — not the lexically-first."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_list_recent", {"subdir": "garden", "limit": 2}, ctx
        )
    paths = [n["path"] for n in out["notes"]]
    assert paths == [
        "garden/entities/deep/baz.md",
        "garden/seedling/bar.md",
    ]


async def test_list_recent_missing_subdir_returns_empty(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """No 500 when subdir doesn't exist — return empty payload."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_list_recent", {"subdir": "garden/does-not-exist"}, ctx
        )
    assert out["total"] == 0
    assert out["notes"] == []


async def test_search_recurses_into_subdirs_by_default(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    """search MUST find content nested under `subdir`, not just direct children."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_search",
            {"query": "searchable widget", "subdir": "garden"},
            ctx,
        )
    assert out["total"] == 1
    assert out["results"][0]["path"] == "garden/entities/foo.md"


async def test_search_recursive_false_skips_subdirs(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_knowledge_search",
            {"query": "searchable widget", "subdir": "garden", "recursive": False},
            ctx,
        )
    assert out["total"] == 0


async def test_list_tags_recurses_into_subdirs_by_default(
    db, workspace_id, user_id, registry, seeded_nested
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_knowledge_list_tags", {"subdir": "garden"}, ctx)
    by_tag = {t["tag"]: t["count"] for t in out["tags"]}
    # entity, seedling, deep all come from sub-dirs; root comes from garden/top.md
    assert by_tag.get("entity") == 1
    assert by_tag.get("seedling") == 1
    assert by_tag.get("deep") == 1
    assert by_tag.get("root") == 1
