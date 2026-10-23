"""A GitHub-delivered product's local ``main`` follows GitHub's, and the diff measures from it.

Real git throughout: a bare repo stands in for GitHub (``file://`` origin), the product repo is
a clone of it, and PRs "merge on GitHub" by pushing to the bare repo from a second clone.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.config import get_settings
from backend.storage.product_workspace import (
    add_run_worktree,
    capture_run_changed_paths,
    capture_run_diff,
    product_workspace_path,
    sync_main_with_origin,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def _git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, f"git {args}: {err.decode()}"
    return out.decode().strip()


class _GitHub:
    """A bare 'GitHub' repo, the server's product clone of it, and a second clone to merge from."""

    def __init__(self, tmp: Path, product_id: uuid.UUID) -> None:
        self.bare = tmp / "github.git"
        self.pusher = tmp / "pusher"
        self.product = product_workspace_path(product_id)

    async def setup(self) -> None:
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        await _git("init", "--bare", "--initial-branch=main", str(self.bare), cwd=self.bare.parent)
        await _git("clone", self.bare.as_uri(), str(self.pusher), cwd=self.bare.parent)
        await self.merge_pr("seed.txt")
        self.product.parent.mkdir(parents=True, exist_ok=True)
        await _git("clone", self.bare.as_uri(), str(self.product), cwd=self.product.parent)

    async def merge_pr(self, name: str) -> str:
        (self.pusher / name).write_text(name)
        await _git("add", "-A", cwd=self.pusher)
        await _git("commit", "-m", f"merge {name}", cwd=self.pusher)
        await _git("push", "origin", "main", cwd=self.pusher)
        return await _git("rev-parse", "HEAD", cwd=self.pusher)


async def _github(tmp_path: Path) -> tuple[uuid.UUID, _GitHub]:
    pid = uuid.uuid4()
    gh = _GitHub(tmp_path, pid)
    await gh.setup()
    return pid, gh


async def test_local_main_fast_forwards_to_github_main(tmp_path: Path) -> None:
    pid, gh = await _github(tmp_path)
    merged = await gh.merge_pr("other-pr.txt")

    assert await sync_main_with_origin(pid) == "fast_forwarded"

    assert await _git("rev-parse", "main", cwd=gh.product) == merged
    assert await sync_main_with_origin(pid) == "up_to_date"


async def test_run_created_after_a_github_merge_branches_from_the_merge_commit(
    tmp_path: Path,
) -> None:
    pid, gh = await _github(tmp_path)
    merged = await gh.merge_pr("other-pr.txt")
    await sync_main_with_origin(pid)

    worktree = await add_run_worktree(pid, uuid.uuid4())

    assert await _git("merge-base", "HEAD", "origin/main", cwd=worktree) == merged


async def test_diff_has_no_files_from_origin_main_after_rebase_onto_it(tmp_path: Path) -> None:
    """Local main lags; the run rebased onto origin/main. Only the run's own file is its change."""
    pid, gh = await _github(tmp_path)
    run_id = uuid.uuid4()
    worktree = await add_run_worktree(pid, run_id)
    (worktree / "mine.py").write_text("x = 1\n")
    await _git("add", "-A", cwd=worktree)
    await _git("commit", "-m", "mine", cwd=worktree)

    await gh.merge_pr("someone-elses.txt")
    await _git("fetch", "origin", cwd=gh.product)  # local main still lags origin/main
    await _git("rebase", "origin/main", cwd=worktree)

    diff = await capture_run_diff(pid, run_id)
    paths = await capture_run_changed_paths(pid, run_id)

    assert diff is not None and "mine.py" in diff
    assert "someone-elses.txt" not in diff
    assert paths == ["mine.py"]


async def test_local_main_with_commits_github_lacks_is_not_fast_forwarded(
    tmp_path: Path,
) -> None:
    pid, gh = await _github(tmp_path)
    (gh.product / "local-only.txt").write_text("x")
    await _git("add", "-A", cwd=gh.product)
    await _git("commit", "-m", "local only", cwd=gh.product)
    local = await _git("rev-parse", "main", cwd=gh.product)
    await gh.merge_pr("other-pr.txt")  # now diverged

    with capture_logs() as logs:
        status = await sync_main_with_origin(pid)

    assert status == "diverged"
    assert await _git("rev-parse", "main", cwd=gh.product) == local
    assert any(entry["event"] == "product_main_diverged_from_origin" for entry in logs)


async def test_local_main_ahead_of_github_is_reported_and_kept(tmp_path: Path) -> None:
    pid, gh = await _github(tmp_path)
    (gh.product / "local-only.txt").write_text("x")
    await _git("add", "-A", cwd=gh.product)
    await _git("commit", "-m", "local only", cwd=gh.product)
    local = await _git("rev-parse", "main", cwd=gh.product)

    with capture_logs() as logs:
        status = await sync_main_with_origin(pid)

    assert status == "ahead"
    assert await _git("rev-parse", "main", cwd=gh.product) == local
    assert any(entry["event"] == "product_main_ahead_of_origin" for entry in logs)


async def test_unreachable_origin_is_reported_not_raised(tmp_path: Path) -> None:
    pid, gh = await _github(tmp_path)
    await _git("remote", "set-url", "origin", (tmp_path / "gone.git").as_uri(), cwd=gh.product)

    assert await sync_main_with_origin(pid) == "fetch_failed"


async def test_product_without_a_github_origin_is_left_alone(tmp_path: Path) -> None:
    pid = uuid.uuid4()
    repo = product_workspace_path(pid)
    repo.mkdir(parents=True)
    await _git("init", "--initial-branch=main", cwd=repo)

    assert await sync_main_with_origin(pid) == "no_origin"
