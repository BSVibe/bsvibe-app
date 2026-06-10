"""MCP bootstrap cancel + retry tool tests — Lift E13.

Exercises ``bsvibe_products_bootstrap_cancel`` and
``bsvibe_products_bootstrap_retry`` end-to-end against an in-memory SQLite
DB. Each test seeds the row it needs, constructs a :class:`ToolContext`
with a deterministic :class:`McpPrincipal`, calls the tool, and asserts
the typed output shape + side effects (DB flips, scheduler call).
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

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
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
        yield


# ---------------------------------------------------------------------------
# bsvibe_products_bootstrap_cancel
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "in_flight_status",
    ["pending", "cloning", "analyzing", "ingesting"],
)
async def test_bootstrap_cancel_flips_in_flight_to_failed(
    db, workspace_id, user_id, registry, seeded, in_flight_status
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://x/y",
                bootstrap_status=in_flight_status,
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_products_bootstrap_cancel",
            {"slug_or_id": "p"},
            ctx,
        )

    assert out["bootstrap_status"] == "failed"
    assert out["bootstrap_error"] == "cancelled by founder"

    async with db() as s:
        row = await s.get(ProductRow, pid)
        assert row is not None
        assert row.bootstrap_status == "failed"
        assert row.bootstrap_error == "cancelled by founder"


@pytest.mark.parametrize(
    "terminal_status",
    ["complete", "failed", "failed:clone", "failed:ingest", "failed:too_large"],
)
async def test_bootstrap_cancel_terminal_status_raises(
    db, workspace_id, user_id, registry, seeded, terminal_status
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://x/y",
                bootstrap_status=terminal_status,
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="terminal"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_cancel",
                {"slug_or_id": "p"},
                ctx,
            )


async def test_bootstrap_cancel_unknown_product_raises(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_cancel",
                {"slug_or_id": "nonexistent"},
                ctx,
            )


async def test_bootstrap_cancel_other_workspace_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(
            ProductRow(
                workspace_id=other_ws,
                name="X",
                slug="x",
                repo_url="https://x/y",
                bootstrap_status="ingesting",
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_cancel",
                {"slug_or_id": "x"},
                ctx,
            )


async def test_bootstrap_cancel_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://x/y",
                bootstrap_status="ingesting",
            )
        )
        await s.commit()

    from backend.mcp.api import ToolScopeDenied

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolScopeDenied):
            await registry.call_tool(
                "bsvibe_products_bootstrap_cancel",
                {"slug_or_id": "p"},
                ctx,
            )


async def test_bootstrap_cancel_attempts_task_cancel(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """When the in-flight task is registered in the runtime, cancel() it.

    The runtime exposes a product_id → Task map for cancellation; the
    tool must call task.cancel() so the running ingest stops promptly.
    """
    import asyncio

    from backend.workflow.application.runtime import product_bootstrap_runtime as rt

    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://x/y",
                bootstrap_status="ingesting",
            )
        )
        await s.commit()

    # Register a long-lived task under this product_id — the cancel tool
    # should cancel() it.
    cancelled = asyncio.Event()

    async def _sleep_forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_sleep_forever())
    rt.register_running_task(pid, task)
    try:
        async with db() as s:
            ctx = ToolContext(
                principal=_principal(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    scopes=("mcp:write",),
                ),
                session=s,
            )
            await registry.call_tool(
                "bsvibe_products_bootstrap_cancel",
                {"slug_or_id": "p"},
                ctx,
            )

        # Give the loop a beat to deliver the cancel.
        for _ in range(50):
            if cancelled.is_set():
                break
            await asyncio.sleep(0.01)
        assert cancelled.is_set(), "in-flight task was not cancelled"
    finally:
        if not task.done():
            task.cancel()
        rt.unregister_running_task(pid)


# ---------------------------------------------------------------------------
# bsvibe_products_bootstrap_retry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "retryable_status",
    ["failed", "failed:clone", "failed:ingest", "failed:too_large", "complete"],
)
async def test_bootstrap_retry_resets_and_schedules(
    db,
    workspace_id,
    user_id,
    registry,
    seeded,
    monkeypatch,
    retryable_status,
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://github.com/org/repo",
                bootstrap_status=retryable_status,
                bootstrap_artifacts_count=42,
                bootstrap_error="prior failure",
                bootstrap_progress={"chunks_done": 5},
            )
        )
        await s.commit()

    captured: dict[str, object] = {}

    def _fake_scheduler(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        "backend.workflow.application.runtime.product_bootstrap_runtime.schedule_product_bootstrap",
        _fake_scheduler,
    )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        out = await registry.call_tool(
            "bsvibe_products_bootstrap_retry",
            {"slug_or_id": "p"},
            ctx,
        )

    assert out["bootstrap_status"] == "pending"
    assert out["bootstrap_artifacts_count"] is None
    assert out["bootstrap_error"] is None
    assert out["bootstrap_progress"] is None

    async with db() as s:
        row = await s.get(ProductRow, pid)
        assert row is not None
        assert row.bootstrap_status == "pending"
        assert row.bootstrap_artifacts_count is None
        assert row.bootstrap_error is None
        assert row.bootstrap_progress is None

    assert captured["product_id"] == pid
    assert captured["workspace_id"] == workspace_id
    assert captured["repo_url"] == "https://github.com/org/repo"


async def test_bootstrap_retry_missing_repo_url_raises(
    db, workspace_id, user_id, registry, seeded
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url=None,
                bootstrap_status="failed",
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        with pytest.raises(ToolError, match="repo_url"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_retry",
                {"slug_or_id": "p"},
                ctx,
            )


@pytest.mark.parametrize("in_flight_status", ["pending", "cloning", "analyzing", "ingesting"])
async def test_bootstrap_retry_in_flight_raises(
    db, workspace_id, user_id, registry, seeded, in_flight_status
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://github.com/org/repo",
                bootstrap_status=in_flight_status,
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        with pytest.raises(ToolError, match="in flight"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_retry",
                {"slug_or_id": "p"},
                ctx,
            )


async def test_bootstrap_retry_unknown_product_raises(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_retry",
                {"slug_or_id": "nonexistent"},
                ctx,
            )


async def test_bootstrap_retry_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://github.com/org/repo",
                bootstrap_status="failed",
            )
        )
        await s.commit()

    from backend.mcp.api import ToolScopeDenied

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
            session_factory=db,
        )
        with pytest.raises(ToolScopeDenied):
            await registry.call_tool(
                "bsvibe_products_bootstrap_retry",
                {"slug_or_id": "p"},
                ctx,
            )


# ---------------------------------------------------------------------------
# Lift E20 — vault_reset_on_retry + confirm_reset two-key wipe
# ---------------------------------------------------------------------------
async def test_bootstrap_retry_vault_reset_requires_confirm(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Refusing without confirm_reset is the documented two-key guard."""
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://github.com/org/repo",
                bootstrap_status="failed",
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        with pytest.raises(ToolError, match="confirm_reset"):
            await registry.call_tool(
                "bsvibe_products_bootstrap_retry",
                {"slug_or_id": "p", "vault_reset_on_retry": True},
                ctx,
            )


async def test_bootstrap_retry_vault_reset_wipes_subtrees(
    db, workspace_id, user_id, registry, seeded, tmp_path, monkeypatch
) -> None:
    """With both flags set, the resettable subtrees are removed before retry."""
    pid = uuid.uuid4()
    async with db() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=workspace_id,
                name="P",
                slug="p",
                repo_url="https://github.com/org/repo",
                bootstrap_status="failed",
            )
        )
        await s.commit()

    # Plant content under each resettable subtree.
    monkeypatch.setenv("BSVIBE_KNOWLEDGE_VAULT_ROOT", str(tmp_path / "vault"))
    get_settings.cache_clear()
    settings = get_settings()
    vault = __import__("pathlib").Path(settings.knowledge_vault_root) / "us-1" / str(workspace_id)
    for sub in ("garden", "concepts", "actions", "proposals", "code_graph"):
        d = vault / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker.md").write_text("x")

    def _noop_scheduler(**_kwargs):
        return None

    monkeypatch.setattr(
        "backend.workflow.application.runtime.product_bootstrap_runtime.schedule_product_bootstrap",
        _noop_scheduler,
    )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
            session_factory=db,
        )
        await registry.call_tool(
            "bsvibe_products_bootstrap_retry",
            {
                "slug_or_id": "p",
                "vault_reset_on_retry": True,
                "confirm_reset": True,
            },
            ctx,
        )

    # All five resettable subtrees should be gone.
    for sub in ("garden", "concepts", "actions", "proposals", "code_graph"):
        assert not (vault / sub).exists(), f"{sub} survived the wipe"
    get_settings.cache_clear()
