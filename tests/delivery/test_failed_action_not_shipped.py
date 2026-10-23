"""A failed github action is not a delivery, and a rebased run branch is re-pushed with a lease.

Run ``f7c5b4d1`` (2026-10-23): the github push was rejected non-fast-forward, telegram went
out, and the run was shipped. These pin both halves against a LOCAL bare repo.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps
from backend.workflow.infrastructure.workers.delivery_worker import _delivery_succeeded

BRANCH = "bsvibe/run-abc123"


async def _git(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def _identity(repo: Path) -> None:
    await _git("config", "user.email", "t@bsvibe.dev", cwd=repo)
    await _git("config", "user.name", "Test", cwd=repo)


async def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", name, cwd=repo)


async def _setup(tmp_path: Path) -> tuple[Path, Path, GitOps]:
    """A bare remote with ``main`` + a full clone of it already holding a pushed run branch."""
    bare = tmp_path / "remote.git"
    await _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    await _git("clone", str(bare), str(seed))
    await _identity(seed)
    await _commit(seed, "README.md")
    await _git("push", "origin", "main", cwd=seed)

    ops = GitOps()
    checkout = tmp_path / "checkout"
    await ops.clone(bare.as_uri(), checkout, token=None, depth=0)
    await ops.checkout_new_branch(checkout, BRANCH)
    await _commit(checkout, "first.txt")
    await ops.push(checkout, BRANCH, token=None)
    return bare, checkout, ops


async def test_rebased_run_branch_is_force_pushed_with_lease(tmp_path: Path) -> None:
    bare, checkout, ops = await _setup(tmp_path)

    # main moves on, the run rebases onto it: the branch diverges from what was pushed.
    advance = tmp_path / "advance"
    await _git("clone", str(bare), str(advance))
    await _identity(advance)
    await _commit(advance, "main-moved.txt")
    await _git("push", "origin", "main", cwd=advance)
    await _git("fetch", "origin", "main", cwd=checkout)
    await _git("rebase", "origin/main", cwd=checkout)

    await ops.push(checkout, BRANCH, token=None)

    assert await _git("rev-parse", BRANCH, cwd=bare) == await _git(
        "rev-parse", "HEAD", cwd=checkout
    )


async def test_lease_refuses_when_remote_has_commits_we_do_not_know(tmp_path: Path) -> None:
    bare, checkout, ops = await _setup(tmp_path)

    # Someone else pushes to the run branch behind our back.
    other = tmp_path / "other"
    await _git("clone", "--branch", BRANCH, str(bare), str(other))
    await _identity(other)
    await _commit(other, "theirs.txt")
    await _git("push", "origin", BRANCH, cwd=other)
    theirs = await _git("rev-parse", BRANCH, cwd=bare)

    # We rewrite our branch (rebase-like) so a plain push would be non-fast-forward anyway.
    await _git("commit", "--amend", "-m", "rewritten", cwd=checkout)

    with pytest.raises(GitError):
        await ops.push(checkout, BRANCH, token=None)
    assert await _git("rev-parse", BRANCH, cwd=bare) == theirs


def _ok(name: str) -> ActionResult:
    return ActionResult(action=name, succeeded=True)


def _failed(name: str) -> ActionResult:
    return ActionResult(
        action=name,
        succeeded=False,
        error="! [rejected] (non-fast-forward)",
    )


def _result(*actions: ActionResult) -> DeliveryResult:
    failed = next((a.error for a in actions if not a.succeeded), None)
    return DeliveryResult(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="code",
        actions=list(actions),
        error=failed,
    )


def test_failed_github_action_is_not_covered_by_a_telegram_success() -> None:
    assert not _delivery_succeeded(_result(_failed("github:outbound"), _ok("telegram:outbound")))


def test_all_actions_succeeding_is_a_delivery() -> None:
    assert _delivery_succeeded(_result(_ok("github:open_pr"), _ok("telegram:outbound")))
