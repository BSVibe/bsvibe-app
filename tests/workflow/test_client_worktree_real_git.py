"""The provisioning command, run against a REAL git repository.

The sibling unit test compares strings. Whether `git worktree add` actually
succeeds twice in a row, and whether the parent checkout really stays clean,
are questions only git can answer — and both are the whole point of the change.

Runs anywhere git does (CI included); no network, no daemon.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from backend.workflow.domain.client_worktree import (
    client_run_worktree,
    worktree_branch,
    worktree_provision_command,
)

_RUN = uuid.UUID("a2c2894a-f0be-491c-a585-7b69eaa972b0")
_TIMEOUT_S = 120


def _sh(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S602 — the command under test IS a shell string
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A founder-shaped checkout: a real repo with a commit and dirty work."""
    root = tmp_path / "BStockReport-client"
    root.mkdir()
    _sh("git init -q -b main", cwd=root)
    _sh("git config user.email t@t && git config user.name t", cwd=root)
    (root / "README.md").write_text("hello\n")
    _sh("git add -A && git commit -qm init", cwd=root)
    # The founder's own work-in-progress — it must survive untouched.
    (root / "README.md").write_text("hello, mid-edit\n")
    return root


def test_the_run_gets_a_real_checkout_of_its_own_branch(repo: Path) -> None:
    result = _sh(worktree_provision_command(str(repo), _RUN))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    worktree = Path(client_run_worktree(str(repo), _RUN))
    assert (worktree / "README.md").exists(), "the worktree must be a real checkout"
    branch = _sh("git rev-parse --abbrev-ref HEAD", cwd=worktree).stdout.strip()
    assert branch == worktree_branch(_RUN)


def test_the_founders_checkout_is_untouched(repo: Path) -> None:
    """Their branch, their uncommitted edit, and a clean ``git status`` — the
    three things the old in-place behaviour took away."""
    _sh(worktree_provision_command(str(repo), _RUN))

    assert _sh("git rev-parse --abbrev-ref HEAD", cwd=repo).stdout.strip() == "main"
    assert (repo / "README.md").read_text() == "hello, mid-edit\n"
    status = _sh("git status --porcelain", cwd=repo).stdout
    assert "wt/" not in status, f"the worktree dir leaked into their status: {status!r}"


def test_provisioning_twice_is_a_no_op(repo: Path) -> None:
    """Runs resume and retry. ``worktree add`` on an existing path is a hard
    error, and a run that dies here never reaches the work it was retrying."""
    first = _sh(worktree_provision_command(str(repo), _RUN))
    assert first.returncode == 0
    (Path(client_run_worktree(str(repo), _RUN)) / "work.txt").write_text("in progress\n")

    second = _sh(worktree_provision_command(str(repo), _RUN))
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert (Path(client_run_worktree(str(repo), _RUN)) / "work.txt").exists(), (
        "a resumed run must find the work it already did, not a fresh empty tree"
    )


def test_a_deleted_worktree_directory_can_be_reprovisioned(repo: Path) -> None:
    """The messy real case: the directory is gone (disk cleanup, an impatient
    ``rm -rf``) but git still has it registered AND the branch still exists.
    Naive ``worktree add`` fails twice over — once on the stale registration,
    once on the existing branch."""
    _sh(worktree_provision_command(str(repo), _RUN))
    _sh(f"rm -rf {client_run_worktree(str(repo), _RUN)}")

    again = _sh(worktree_provision_command(str(repo), _RUN))
    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"
    assert Path(client_run_worktree(str(repo), _RUN)).is_dir()


def test_two_runs_get_independent_checkouts(repo: Path) -> None:
    """The concurrency the founder asked for: two runs on one product editing
    the same files without a boundary is the state this replaces."""
    other = uuid.UUID("b09f0920-05d1-41aa-987b-7b745aa4e4d4")
    assert _sh(worktree_provision_command(str(repo), _RUN)).returncode == 0
    assert _sh(worktree_provision_command(str(repo), other)).returncode == 0

    a = Path(client_run_worktree(str(repo), _RUN))
    b = Path(client_run_worktree(str(repo), other))
    (a / "README.md").write_text("run A\n")
    (b / "README.md").write_text("run B\n")
    assert (a / "README.md").read_text() == "run A\n"
    assert (b / "README.md").read_text() == "run B\n"


# ── the wiring: the run must actually be pointed at its worktree ─────────────


@pytest.mark.asyncio
async def test_the_box_provisions_the_worktree_and_roots_itself_there() -> None:
    """Derived in one place, used in two: the agent's dispatch runs its CLI with
    this as cwd, and the verification box reads from it. And the directory has
    to EXIST before the first agent turn — which is why provisioning happens at
    acquire, the step that already precedes the drive loop."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxManager,
    )

    dispatched: list[tuple[str, str]] = []

    class _Session:
        workspace_mount = "/repo"

        async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> object:
            from backend.workflow.infrastructure.sandbox import SandboxResult

            dispatched.append((command, self.workspace_mount))
            return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    manager = ClientWorkerSandboxManager(
        redis=object(),
        session_factory=object(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        pinned_worker_id=None,
        client_workspace_dir="/Users/founder/proj",
        default_timeout_s=60.0,
        run_id=_RUN,
    )
    manager._session_for = lambda path: _Session_at(path, dispatched)  # type: ignore[attr-defined]

    box = await manager.acquire(uuid.uuid4(), "/server/side/path")

    assert box.workspace_mount == client_run_worktree("/Users/founder/proj", _RUN)
    provision, ran_in = dispatched[0]
    assert "worktree add" in provision
    assert ran_in == "/Users/founder/proj", (
        "provisioning must run in the REPO — the worktree does not exist yet"
    )


def _Session_at(path: str, log: list[tuple[str, str]]) -> object:  # noqa: N802
    class _S:
        workspace_mount = path

        async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> object:
            from backend.workflow.infrastructure.sandbox import SandboxResult

            log.append((command, path))
            return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    return _S()
