"""Runtime layer — SqlAlchemy repository + job error paths."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.workflow.application.runtime.product_bootstrap_runtime as bootstrap_runtime
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    STATUS_FAILED_CLONE,
    SqlAlchemyBootstrapRepository,
    run_product_bootstrap_job,
)
from backend.workflow.domain.gate_discovery import discover_gate
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps

from .._support import db_engine

pytestmark = pytest.mark.asyncio


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_out(repo: Path, *args: str) -> str:
    """Sync git helper returning stdout (keeps blocking subprocess out of the
    async test bodies — ruff ASYNC221)."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(repo: Path, files: dict[str, str]) -> None:
    """Create a real git repo at ``repo`` with ``files`` in an initial commit."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def _clone_stub(files: dict[str, str]):
    """A GitOps.clone stand-in that materializes a real git repo at ``dest``
    (so the real scaffold write + commit_all runs against actual git)."""

    async def _clone(repo_url, dest, *, token, depth=1):  # noqa: ANN001, ARG001
        _init_repo(Path(dest), files)
        # Mirror GitOps.clone's committer identity setup.
        _git(Path(dest), "config", "user.email", "agent@bsvibe.dev")
        _git(Path(dest), "config", "user.name", "BSVibe Agent")

    return _clone


async def _seed_product(session_factory, workspace_id: uuid.UUID, product_id: uuid.UUID) -> None:
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


# --------------------------------------------------------------------------
# I1c — bootstrap scaffolds a minimal acceptance gate when the repo has none
# --------------------------------------------------------------------------


async def test_bootstrap_scaffolds_gate_for_gateless_python_repo(
    session_factory, tmp_path, monkeypatch
):
    """A cloned Python repo that declares NO gate gets a minimal CI scaffolded +
    committed to main, so I1 has the target's own gate to run. Drives the real
    job wiring with a real git repo; the heavy ingest tail is short-circuited by
    making knowledge-build return None (→ failed:ingest AFTER scaffold)."""
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    object.__setattr__(settings, "product_workspace_root", str(tmp_path))

    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed_product(session_factory, workspace_id, product_id)

    git = GitOps()
    git.clone = _clone_stub({"pyproject.toml": "[project]\nname='x'\n"})  # type: ignore[method-assign]
    # Stop right after scaffolding — no LLM account needed.
    monkeypatch.setattr(bootstrap_runtime, "build_bootstrap_knowledge", lambda **_: None)

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=git,
    )

    repo_path = tmp_path / str(product_id)
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    assert ci.is_file(), "gate should be scaffolded"
    # …and discoverable as the repo's own gate.
    assert not discover_gate(repo_path).is_empty
    # …and committed (working tree clean — nothing left unstaged).
    assert _git_out(repo_path, "status", "--porcelain").strip() == ""


async def test_bootstrap_does_not_scaffold_when_repo_has_a_gate(
    session_factory, tmp_path, monkeypatch
):
    """A cloned repo that already declares its own CI is left untouched — no
    scaffold commit clobbers the project's own definition of done."""
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    object.__setattr__(settings, "product_workspace_root", str(tmp_path))

    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed_product(session_factory, workspace_id, product_id)

    existing_ci = "jobs:\n  j:\n    steps:\n      - run: ruff check .\n"
    git = GitOps()
    git.clone = _clone_stub(  # type: ignore[method-assign]
        {"pyproject.toml": "[project]\nname='x'\n", ".github/workflows/ci.yml": existing_ci}
    )
    monkeypatch.setattr(bootstrap_runtime, "build_bootstrap_knowledge", lambda **_: None)

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=git,
    )

    repo_path = tmp_path / str(product_id)
    # The user's CI is unchanged, and there is exactly ONE commit (the seed) —
    # no scaffold commit was added.
    assert (repo_path / ".github" / "workflows" / "ci.yml").read_text() == existing_ci
    assert len(_git_out(repo_path, "log", "--oneline").strip().splitlines()) == 1
