"""A client_attach run commits its own work before it settles.

#723 turned git delivery off for this execution model on the reasoning that
"the server never clones such a product, so there is no checkout to commit
from". True about the server, wrong conclusion — the same one in-place verify
already overturned for the gate. The checkout is on the founder's machine, the
exec channel reaches it, and since #734 the run has a worktree of its own.

The hole left every client_attach run's work UNCOMMITTED in that tree: changes
from different runs piling up unattributably, and a cancelled run's files read
by a later session as "that run produced nothing" when it had produced
everything.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.application.client_attach_delivery import (
    commit_and_push_run_work,
    commit_subject,
)
from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio


class _Box:
    """The founder's machine, rooted in this run's worktree."""

    def __init__(self, *, exits: dict[str, int] | None = None, raises: str | None = None) -> None:
        self.commands: list[str] = []
        self._exits = exits or {}
        self._raises = raises

    @property
    def workspace_mount(self) -> str:
        return "/Users/founder/proj/wt/a2c2894a"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.commands.append(command)
        if self._raises and self._raises in command:
            raise RuntimeError("no live client worker for this workspace")
        code = next((c for k, c in self._exits.items() if k in command), 0)
        return SandboxResult(exit_code=code, stdout="", stderr="boom", timed_out=False)


class _Run:
    def __init__(self, intent: str = "표면 하네스를 만들어줘") -> None:
        self.id = uuid.UUID("a2c2894a-f0be-491c-a585-7b69eaa972b0")
        self.payload: dict[str, Any] = {"intent_text": intent}


async def test_the_work_is_committed_and_pushed_on_the_runs_branch() -> None:
    # `git diff --cached --quiet` exits 1 when there ARE staged changes.
    box = _Box(exits={"diff --cached": 1})

    outcome = await commit_and_push_run_work(box=box, run=_Run())

    assert outcome.committed is True
    assert outcome.pushed is True
    assert outcome.branch == "run/a2c2894a"
    assert any(c.startswith("git commit -m") for c in box.commands), box.commands
    assert any("git push -u origin run/a2c2894a" in c for c in box.commands), box.commands


async def test_the_commit_names_the_run() -> None:
    """A commit nobody can attribute is what made a tree of half-finished work
    unreadable in the first place."""
    subject = commit_subject(_Run("표면 하네스를 만들어줘"))

    assert "run-a2c2894a" in subject
    assert "표면 하네스" in subject
    assert "\n" not in subject


async def test_a_run_that_changed_nothing_makes_no_commit() -> None:
    """An empty commit would be a claim that the run did something."""
    box = _Box()  # diff --cached exits 0 → nothing staged

    outcome = await commit_and_push_run_work(box=box, run=_Run())

    assert outcome.committed is False
    assert outcome.pushed is False
    assert outcome.error is None, "nothing to commit is honest, not a failure"
    assert not any("git push" in c for c in box.commands), box.commands


async def test_no_token_is_ever_put_on_the_wire() -> None:
    """The founder's own git credentials do the push (their decision): the
    source is theirs and no server-side token travels to their machine."""
    box = _Box(exits={"diff --cached": 1})

    await commit_and_push_run_work(box=box, run=_Run())

    for command in box.commands:
        assert "x-access-token" not in command
        assert "remote set-url" not in command, "the server must not rewrite their origin"


async def test_a_failed_push_still_lets_the_run_settle_and_says_so() -> None:
    """The work is committed and safe on a named branch, so a lost push is
    recoverable — but silence would put the founder back to not knowing where
    their work is."""
    box = _Box(exits={"diff --cached": 1, "git push": 1})

    outcome = await commit_and_push_run_work(box=box, run=_Run())

    assert outcome.committed is True
    assert outcome.pushed is False
    assert outcome.error is not None


async def test_an_unreachable_machine_is_recorded_not_raised() -> None:
    """The gate has already passed by this point. A dead worker must not turn a
    finished run into a crash."""
    box = _Box(raises="git add")

    outcome = await commit_and_push_run_work(box=box, run=_Run())

    assert outcome.committed is False
    assert outcome.error is not None and "no live client worker" in outcome.error


async def test_build_litter_is_not_committed() -> None:
    """The agent's own tools may leave a venv or caches in the worktree. The two
    execution models must ship the same shape, and the server-side delivery
    excludes exactly these."""
    box = _Box(exits={"diff --cached": 1})

    await commit_and_push_run_work(box=box, run=_Run())

    staging = " ".join(c for c in box.commands if c.startswith(("git add", "git reset")))
    for litter in (".venv", "node_modules", "__pycache__"):
        assert litter in staging, staging
    assert "(exclude)" not in staging, (
        "an `add` pathspec that NAMES an ignored path makes git refuse the whole "
        f"staging step (exit 1) — measured on git 2.52: {staging!r}"
    )


# ── against real git: the ladder has to actually produce a commit ────────────


async def test_the_commands_really_commit_in_a_real_worktree(tmp_path: object) -> None:
    """The tests above compare strings. Whether this sequence produces a commit
    on the run's branch — with the founder's own identity, and without touching
    their checkout — is a question only git answers."""
    import subprocess
    from pathlib import Path

    from backend.workflow.domain.client_worktree import (
        client_run_worktree,
        worktree_provision_command,
    )

    run = _Run()
    root = Path(str(tmp_path)) / "proj"
    root.mkdir()

    def sh(cmd: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S602 — the command under test IS a shell string
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
        )

    sh("git init -q -b main && git config user.email t@t && git config user.name t", root)
    (root / "README.md").write_text("hello\n")
    # ⚠️ THE CONDITION THAT WAS MISSING. Real repos ignore their own build
    # litter, and naming an ignored path in an `add` pathspec is what git
    # refuses ("The following paths are ignored by one of your .gitignore
    # files", exit 1). Without this file the fixture created a `.venv` that was
    # merely untracked, the exclusion pathspecs matched nothing ignored, and the
    # staging step passed here while failing in production — live run
    # `2abd398e` on BStockReport, whose `.gitignore` has all three.
    (root / ".gitignore").write_text(".venv/\n.pytest_cache/\n.ruff_cache/\n")
    sh("git add -A && git commit -qm init", root)
    sh(worktree_provision_command(str(root), run.id), root)

    worktree = Path(client_run_worktree(str(root), run.id))
    (worktree / "new_file.py").write_text("x = 1\n")
    # Ignored litter — what a native `uv run` leaves behind in the tree the
    # agent works in, and the shape that broke staging.
    for name in (".venv", ".pytest_cache", ".ruff_cache"):
        (worktree / name).mkdir()
        (worktree / name / "junk").write_text("binary\n")
    # NOT ignored by this repo: the exclusion has to keep earning its place, or
    # the fix would be "drop the exclusions and let .gitignore do it" — which
    # ships node_modules for every repo that forgot to ignore it.
    (worktree / "node_modules").mkdir()
    (worktree / "node_modules" / "left-pad.js").write_text("//\n")

    class _RealBox:
        @property
        def workspace_mount(self) -> str:
            return str(worktree)

        async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> Any:
            done = sh(command, worktree)
            return SandboxResult(
                exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr, timed_out=False
            )

    outcome = await commit_and_push_run_work(box=_RealBox(), run=run)

    assert outcome.committed is True
    assert outcome.pushed is False, "no remote in this fixture — recorded, not raised"
    log = sh("git log --oneline -1", worktree).stdout
    assert "run-a2c2894a" in log, log
    files = sh("git show --name-only --format= HEAD", worktree).stdout
    assert "new_file.py" in files
    assert ".venv" not in files, f"build litter was committed: {files!r}"
    assert "node_modules" not in files, (
        f"litter this repo does NOT ignore was committed — .gitignore alone is "
        f"not enough: {files!r}"
    )
    assert sh("git status --porcelain", root).stdout == "", "the founder's checkout must be clean"
