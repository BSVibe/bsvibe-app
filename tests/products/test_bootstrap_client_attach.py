"""#692 — bootstrap never clones a client_attach product's source.

Choosing ``client_attach`` is the founder saying "use BSVibe's orchestration,
but my source stays on my machine". Bootstrap's whole job is clone → walk →
ingest into the server-side knowledge vault, which is exactly the copy that
contract forbids. So for a client_attach product the job must stop BEFORE the
clone and record an honest terminal status — never ``complete`` (nothing was
ingested) and never ``failed`` (nothing went wrong).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.workflow.application.runtime.product_bootstrap_runtime as rt
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session_factory, *, metadata: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=False))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug=f"p-{product_id.hex[:8]}",
                repo_url="https://x/y",
                product_metadata=metadata,
            )
        )
        await s.commit()
    return workspace_id, product_id


async def test_bootstrap_skips_clone_for_client_attach_product(
    session_factory, tmp_path: Path
) -> None:
    """No clone, no ingest — and an honest ``skipped:client_attach`` status."""
    workspace_id, product_id = await _seed(
        session_factory,
        metadata={
            "execution_target": "client_attach",
            "client_workspace_path": str(tmp_path / "proj"),
        },
    )
    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    await rt.run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    fake_git.clone.assert_not_awaited()
    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == rt.STATUS_SKIPPED_CLIENT_ATTACH, (
            "a client_attach product's source must not be cloned/ingested server-side "
            f"(status={row.bootstrap_status!r})"
        )


async def test_bootstrap_still_clones_for_server_sandbox_product(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default path is untouched — the clone is still attempted."""
    workspace_id, product_id = await _seed(session_factory, metadata={})
    settings = rt.get_settings()
    product_root = tmp_path / "ws"
    product_root.mkdir()
    object.__setattr__(settings, "product_workspace_root", str(product_root))

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()
    # Stop right after the clone — this test only pins that cloning still happens.
    monkeypatch.setattr(rt, "build_bootstrap_knowledge", lambda **_kw: None)

    await rt.run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    fake_git.clone.assert_awaited()
