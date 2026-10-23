"""A run's baseline is where its branch left main — not wherever HEAD happens to be.

Run ``5f677182`` (PR #1207) was reopened after a rejection. ``capture_inplace_baseline`` read
``HEAD`` at the top of the second attempt, and by then HEAD already carried the run's OWN first
attempt — so ``baseline..HEAD`` erased it from the changed-file list. A ``.py`` committed the first
time round vanished from the gate's inputs once only the docs were being fixed. Rebasing the run
onto a moved main is the mirror failure: a baseline taken from the wrong side drags main's commits
in. ``main...HEAD`` (the merge-base) is the one meaning that gets both right.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from backend.workflow.application.inplace_gate import (
    capture_run_baseline,
    changed_paths,
    current_run_baseline,
    reconcile_written_paths,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("docs\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-qb", "run/5f677182")
    return tmp_path


def _head(repo: Path) -> str:
    out = subprocess.run(  # noqa: S603, S607
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, body: str = "x = 1\n") -> None:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"add {name}")


async def test_a_reopened_run_keeps_its_own_earlier_commit(repo: Path) -> None:
    _commit(repo, "engine.py")  # the first attempt
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))

    baseline = await capture_run_baseline(box)  # the second attempt begins
    (repo / "README.md").write_text("fixed docs\n")  # ...and only fixes the docs

    assert await changed_paths(box, baseline) == ["README.md", "engine.py"]
    assert await reconcile_written_paths(box, baseline, ["README.md"]) == [
        "README.md",
        "engine.py",
    ]


async def test_a_rebased_run_does_not_inherit_mains_other_commits(repo: Path) -> None:
    _commit(repo, "engine.py")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "elsewhere.py")  # somebody else's merged work
    _git(repo, "checkout", "-q", "run/5f677182")
    _git(repo, "rebase", "-q", "main")
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))

    baseline = await capture_run_baseline(box)

    assert await changed_paths(box, baseline) == ["engine.py"]


async def test_a_fresh_run_baseline_is_head(repo: Path) -> None:
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    head = _head(repo)

    assert await capture_run_baseline(box) == head


async def test_without_a_main_ref_the_baseline_falls_back_to_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "trunk")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(tmp_path))
    head = _head(tmp_path)

    assert await capture_run_baseline(box) == head


async def test_a_rebase_after_the_baseline_was_taken_does_not_drag_mains_commits_in(
    repo: Path,
) -> None:
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    start = await capture_run_baseline(box)  # the drive begins on the old branch point
    _commit(repo, "engine.py")  # the run's first attempt
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "elsewhere.py")  # somebody else's merged work
    _git(repo, "checkout", "-q", "run/5f677182")
    _git(repo, "rebase", "-q", "main")  # mid-drive rebase

    baseline = await current_run_baseline(box, start)

    assert await changed_paths(box, baseline) == ["engine.py"]
    assert await reconcile_written_paths(box, baseline, ["engine.py"]) == ["engine.py"]
    assert await changed_paths(box, start) == ["elsewhere.py", "engine.py"]  # the old reading


async def test_without_a_rebase_the_current_baseline_is_the_one_taken_at_the_start(
    repo: Path,
) -> None:
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    start = await capture_run_baseline(box)
    _commit(repo, "engine.py")

    assert await current_run_baseline(box, start) == start
    assert await changed_paths(box, await current_run_baseline(box, start)) == ["engine.py"]


async def test_when_no_branch_point_exists_the_start_baseline_is_kept(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "trunk")
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(tmp_path))

    assert await current_run_baseline(box, "abc123") == "abc123"
