"""Runtime layer — SqlAlchemy repository + job error paths."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    STATUS_FAILED_CLONE,
    SqlAlchemyBootstrapRepository,
    run_product_bootstrap_job,
)
from backend.workflow.infrastructure.delivery.git_ops import GitError

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def test_sqlalchemy_repository_marks_status(session_factory):
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=True))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()

    repo = SqlAlchemyBootstrapRepository(session_factory)
    run_id = uuid.uuid4()
    await repo.mark_status(product_id, status="cloning", run_id=run_id)
    await repo.mark_status(product_id, status="complete", artifacts_count=42)

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == "complete"
        assert row.bootstrap_run_id == run_id
        assert row.bootstrap_artifacts_count == 42

    progress = await repo.fetch_progress(product_id, workspace_id=workspace_id)
    assert progress is not None
    assert progress.status == "complete"
    assert progress.artifacts_count == 42


async def test_sqlalchemy_repository_fetch_progress_workspace_scoped(session_factory):
    """A product in a different workspace returns ``None`` from fetch_progress."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    product_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
        s.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=ws_a, name="p", slug="p"))
        await s.commit()

    repo = SqlAlchemyBootstrapRepository(session_factory)
    assert await repo.fetch_progress(product_id, workspace_id=ws_b) is None


async def test_run_job_writes_failed_clone_on_git_error(session_factory, tmp_path):
    """Job lifecycle: clone fails → status row reads ``failed:clone``."""
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=True))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()

    # Point the product workspace root at tmp_path so the job's mkdir/clone
    # path stays inside the test sandbox.
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    object.__setattr__(settings, "product_workspace_root", str(tmp_path))

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("simulated clone failure"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://invalid.example/repo.git",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_FAILED_CLONE
        assert row.bootstrap_error is not None
        assert "GitError" in row.bootstrap_error


async def test_run_job_writes_failed_when_workspace_missing(session_factory, tmp_path):
    """Job lifecycle: workspace_id not in DB → ``failed:ingest`` (defensive).

    PG ON DELETE CASCADE makes "delete workspace, keep product" impossible;
    instead, the runner is called with a bogus ``workspace_id`` while the
    product lives under a real workspace. The defensive branch in
    :func:`run_product_bootstrap_job` looks up the runner-supplied
    workspace_id and finds nothing, then marks ``failed:ingest`` on the
    product row (``mark_status`` does not scope by workspace).
    """
    real_workspace_id = uuid.uuid4()
    bogus_workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    async with session_factory() as s:
        s.add(WorkspaceRow(id=real_workspace_id, name="t", region="us-1", safe_mode=True))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=real_workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=bogus_workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == "failed:ingest"
        assert row.bootstrap_error is not None
