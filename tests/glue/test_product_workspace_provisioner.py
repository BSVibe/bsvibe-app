"""W1 glue — product workspace provisioner end-to-end.

The unit tests in ``tests/storage/test_product_workspace.py`` cover the
filesystem-level invariants of init/add/remove. THIS test wires the
provisioner through the same composer the production
:func:`build_agent_execution_deps` factory uses, so a regression in the
github > product > legacy priority chain surfaces here.

Drives the real composite provisioner against ``tmp_path`` with a
hand-rolled stub for the github branch (the github clone path needs a
real remote; we just confirm it's tried first and falls through when
there's no binding).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.execution.db import ExecutionRun, RunStatus
from backend.storage.product_workspace import (
    init_product_workspace,
    product_workspace_path,
    run_branch_name,
    run_worktree_path,
)
from backend.workers.run import (
    _build_composite_workspace_provisioner,
    _product_workspace_provisioner,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "product_workspace_root",
        str(tmp_path / "products"),
        raising=False,
    )
    monkeypatch.setattr(
        get_settings(),
        "run_workspace_root",
        str(tmp_path / "runs"),
        raising=False,
    )


def _make_run(*, product_id: uuid.UUID | None) -> ExecutionRun:
    """Build a fully-populated ExecutionRun without going through the DB."""
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=product_id,
        request_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={},
    )


async def _gh_noop(_session, _run, _workspace_dir) -> None:
    """github branch stub that does nothing — used to simulate "no github
    binding" so the composer falls through to the product branch."""
    return None


async def _gh_fills(_session, _run, workspace_dir: Path) -> None:
    """github branch stub that fills the dir — used to verify the composer
    short-circuits the product branch when github already did work."""
    if workspace_dir.exists() and not any(workspace_dir.iterdir()):
        workspace_dir.rmdir()
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "from-github.txt").write_text("from github stub")


# ---------------------------------------------------------------------------
# Product workspace provisioner — direct
# ---------------------------------------------------------------------------


async def test_product_provisioner_creates_worktree_for_run_with_product() -> None:
    product_id = uuid.uuid4()
    run = _make_run(product_id=product_id)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)  # AgentWorker creates an empty dir first

    provisioned = await _product_workspace_provisioner(None, run, workspace_dir)
    assert provisioned is True

    # Worktree exists and is on the run branch.
    assert workspace_dir.exists()
    assert (workspace_dir / ".git").exists()  # worktree pointer file
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        cwd=str(workspace_dir),
    )
    out, _ = await proc.communicate()
    assert out.decode().strip() == run_branch_name(run.id)


async def test_product_provisioner_no_op_for_run_without_product() -> None:
    """Legacy / Direct-path tests that mint a run without a product_id
    must still work — the provisioner reports False and the empty dir is
    left for whoever wants it."""
    run = _make_run(product_id=None)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)

    provisioned = await _product_workspace_provisioner(None, run, workspace_dir)
    assert provisioned is False

    # Dir is still there and empty (no worktree wiring happened).
    assert workspace_dir.exists()
    assert list(workspace_dir.iterdir()) == []


async def test_product_provisioner_lazy_inits_workspace() -> None:
    """A product that exists but whose workspace has never been initialised
    (legacy / pre-W1 product row) lazily inits on first run start. Avoids
    needing a separate startup-time backfill."""
    product_id = uuid.uuid4()
    # Don't pre-init; the provisioner must do it.
    assert not (product_workspace_path(product_id) / ".git").exists()

    run = _make_run(product_id=product_id)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)
    await _product_workspace_provisioner(None, run, workspace_dir)

    assert (product_workspace_path(product_id) / ".git").is_dir()


# ---------------------------------------------------------------------------
# Composite provisioner — priority chain
# ---------------------------------------------------------------------------


async def test_composite_falls_through_github_no_op_into_product() -> None:
    """github stub is a no-op (no binding) → product provisioner runs and
    creates the worktree."""
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    run = _make_run(product_id=product_id)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)

    composite = _build_composite_workspace_provisioner(
        github=_gh_noop,
        product=_product_workspace_provisioner,
    )
    await composite(None, run, workspace_dir)

    assert (workspace_dir / ".git").exists()  # product worktree marker


async def test_composite_short_circuits_when_github_fills_dir() -> None:
    """github stub puts a file in the dir → product provisioner is NOT
    invoked (the dir is no longer empty). Locks in priority order."""
    product_id = uuid.uuid4()
    await init_product_workspace(product_id)
    run = _make_run(product_id=product_id)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)

    composite = _build_composite_workspace_provisioner(
        github=_gh_fills,
        product=_product_workspace_provisioner,
    )
    await composite(None, run, workspace_dir)

    # github stub's marker is here, NOT the worktree pointer.
    assert (workspace_dir / "from-github.txt").exists()
    # The worktree wasn't created — the run branch must NOT exist.
    proc = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--verify",
        run_branch_name(run.id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(product_workspace_path(product_id)),
    )
    await proc.communicate()
    assert proc.returncode != 0, "product branch should NOT exist when github handled it"


async def test_composite_leaves_empty_dir_when_neither_applies() -> None:
    """No github binding (stub no-op) + no product_id on the run → empty
    dir survives (matches the Direct-path / no-product test invariant)."""
    run = _make_run(product_id=None)
    workspace_dir = run_worktree_path(run.id)
    workspace_dir.mkdir(parents=True)

    composite = _build_composite_workspace_provisioner(
        github=_gh_noop,
        product=_product_workspace_provisioner,
    )
    await composite(None, run, workspace_dir)

    assert workspace_dir.exists()
    assert list(workspace_dir.iterdir()) == []
