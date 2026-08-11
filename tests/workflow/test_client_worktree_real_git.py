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
from typing import Any

import pytest

from backend.workflow.domain.client_worktree import (
    RECLAIM_HELD,
    client_run_worktree,
    worktree_branch,
    worktree_provision_command,
    worktree_reclaim_command,
)
from backend.workflow.infrastructure.sandbox.errors import SandboxError

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


# ── reclaiming it afterwards ─────────────────────────────────────────────────
#
# Only git can answer the question this whole design rests on: WHEN does
# `worktree remove` refuse? String tests would freeze our belief about it; these
# freeze git's behaviour. (Measured on 2.52: it refuses on modified or untracked
# files, and does NOT refuse on ignored ones.)


def _commit_in(worktree: Path, message: str) -> str:
    _sh("git config user.email t@t && git config user.name t", cwd=worktree)
    _sh(f"git add -A && git commit -qm {message}", cwd=worktree)
    return _sh("git rev-parse HEAD", cwd=worktree).stdout.strip()


def test_a_finished_runs_worktree_is_given_back(repo: Path) -> None:
    _sh(worktree_provision_command(str(repo), _RUN))
    worktree = Path(client_run_worktree(str(repo), _RUN))
    (worktree / "made.txt").write_text("the run's work\n")
    _commit_in(worktree, "work")

    result = _sh(worktree_reclaim_command(str(repo), _RUN))

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not worktree.exists(), "a whole checkout per run, kept forever, fills the disk"


def test_committed_work_survives_being_reclaimed(repo: Path) -> None:
    """The reason reclaiming a committed tree is safe at all: removal touches
    neither the branch nor the objects. So a run whose PUSH failed still has its
    work — on its branch, in the founder's own repo — after the directory is
    gone. Without this, the reaper would be the data loss it exists to prevent.
    """
    _sh(worktree_provision_command(str(repo), _RUN))
    worktree = Path(client_run_worktree(str(repo), _RUN))
    (worktree / "deliverable.txt").write_text("the whole point\n")
    sha = _commit_in(worktree, "work")

    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0

    branch = worktree_branch(_RUN)
    assert _sh(f"git rev-parse --verify -q {branch}", cwd=repo).stdout.strip() == sha
    recovered = _sh(f"git show {branch}:deliverable.txt", cwd=repo).stdout
    assert recovered == "the whole point\n", "the run's work must be recoverable afterwards"


def test_uncommitted_work_is_never_thrown_away(repo: Path) -> None:
    """The case that decides ``--force``.

    A run cancelled mid-flight leaves work that exists in exactly one place. A
    reaper that deletes it is worse than no reaper — that loss is what #734/#735
    were built to stop. git refuses on its own; this test is what keeps someone
    from "fixing" the refusal by forcing past it.
    """
    _sh(worktree_provision_command(str(repo), _RUN))
    worktree = Path(client_run_worktree(str(repo), _RUN))
    (worktree / "half-done.txt").write_text("hours of work\n")

    result = _sh(worktree_reclaim_command(str(repo), _RUN))

    assert result.returncode == RECLAIM_HELD, (
        f"holding a tree with work must be its own outcome, not a generic failure: {result!r}"
    )
    assert (worktree / "half-done.txt").read_text() == "hours of work\n"


def test_a_modified_tracked_file_holds_the_worktree_too(repo: Path) -> None:
    """Untracked files are the obvious case; an edit to a tracked file is the
    same loss and git treats it the same way."""
    _sh(worktree_provision_command(str(repo), _RUN))
    worktree = Path(client_run_worktree(str(repo), _RUN))
    (worktree / "README.md").write_text("edited by the run\n")

    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == RECLAIM_HELD
    assert (worktree / "README.md").read_text() == "edited by the run\n"


def test_build_litter_does_not_hold_the_worktree(repo: Path) -> None:
    """``.venv`` and ``node_modules`` are the bulk of the disk and none of the
    value. If ignored files held a tree, every run that installed anything would
    pin its checkout forever and the reaper would reclaim almost nothing."""
    _sh("printf '.venv/\\n' > .gitignore && git add -A && git commit -qm ignore", cwd=repo)
    _sh(worktree_provision_command(str(repo), _RUN))
    worktree = Path(client_run_worktree(str(repo), _RUN))
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")

    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0
    assert not worktree.exists()


def test_reclaiming_twice_is_a_no_op(repo: Path) -> None:
    """Runs resume, retry, and crash. A reaper that fails on an already-clean
    machine turns every such path into a false alarm."""
    _sh(worktree_provision_command(str(repo), _RUN))
    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0

    again = _sh(worktree_reclaim_command(str(repo), _RUN))
    assert again.returncode == 0, f"{again.stdout}\n{again.stderr}"


def test_nothing_to_reclaim_is_not_a_failure(repo: Path) -> None:
    """A run that never provisioned (it failed before ``acquire``) still reaches
    the release step."""
    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0


def test_a_hand_deleted_directory_leaves_no_stale_registration(repo: Path) -> None:
    """The founder cleans up by hand. Reclaim must then finish the job git left
    half-done, or the next run's ``worktree add`` meets a path git believes is
    taken."""
    _sh(worktree_provision_command(str(repo), _RUN))
    _sh(f"rm -rf {client_run_worktree(str(repo), _RUN)}")

    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0
    registered = _sh("git worktree list --porcelain", cwd=repo).stdout
    assert "a2c2894a" not in registered, f"stale registration left behind: {registered!r}"


def test_the_founders_own_checkout_survives_the_reaper(repo: Path) -> None:
    """The blast radius question, asked of the reaper rather than the provisioner."""
    _sh(worktree_provision_command(str(repo), _RUN))
    _sh(worktree_reclaim_command(str(repo), _RUN))

    assert (repo / "README.md").read_text() == "hello, mid-edit\n"
    assert _sh("git rev-parse --abbrev-ref HEAD", cwd=repo).stdout.strip() == "main"


def test_one_runs_reclaim_leaves_another_runs_worktree_alone(repo: Path) -> None:
    """Two runs on one product is the concurrency #734 bought. A reaper scoped
    to the wrong thing would take it straight back."""
    other = uuid.UUID("b09f0920-05d1-41aa-987b-7b745aa4e4d4")
    _sh(worktree_provision_command(str(repo), _RUN))
    _sh(worktree_provision_command(str(repo), other))

    assert _sh(worktree_reclaim_command(str(repo), _RUN)).returncode == 0
    assert Path(client_run_worktree(str(repo), other)).is_dir()


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


def _manager(run_id: uuid.UUID | None, dispatched: list[tuple[str, str]]) -> Any:
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxManager,
    )

    manager = ClientWorkerSandboxManager(
        redis=object(),
        session_factory=object(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        pinned_worker_id=None,
        client_workspace_dir="/Users/founder/proj",
        default_timeout_s=60.0,
        run_id=run_id,
    )
    manager._session_for = lambda path: _Session_at(path, dispatched)  # type: ignore[attr-defined]
    return manager


@pytest.mark.asyncio
async def test_release_gives_the_runs_worktree_back() -> None:
    """``release`` is where this belongs: ``agent_loop`` calls it in a ``finally``,
    so it is the ONE seam every terminal path goes through — verified, refuted,
    cancelled, crashed. Hanging the reaper off the verified settle instead would
    reclaim nothing from exactly the runs that end badly, which are the ones
    that leave debris."""
    dispatched: list[tuple[str, str]] = []

    await _manager(_RUN, dispatched).release(uuid.uuid4())

    assert dispatched, "nothing was reclaimed — #734 makes a checkout per run and this frees it"
    command, ran_in = dispatched[0]
    assert "worktree remove" in command
    assert ran_in == "/Users/founder/proj", (
        "reclaim must run in the REPO — you cannot stand in the directory you are removing"
    )


@pytest.mark.asyncio
async def test_release_never_takes_down_a_finished_run() -> None:
    """It runs inside ``agent_loop``'s ``finally``. An exception raised here
    replaces whatever the run was about to report — so an unreachable machine
    would turn a proved run into a crash. Reclaiming disk is never worth that.
    """
    dispatched: list[tuple[str, str]] = []
    manager = _manager(_RUN, dispatched)
    manager._session_for = lambda path: _Unreachable()  # type: ignore[attr-defined]

    await manager.release(uuid.uuid4())  # must not raise


@pytest.mark.asyncio
async def test_a_run_with_no_worktree_reclaims_nothing() -> None:
    """No ``run_id`` means the box was rooted at the founder's own checkout.
    Reclaiming there would delete THEIR directory."""
    dispatched: list[tuple[str, str]] = []

    await _manager(None, dispatched).release(uuid.uuid4())

    assert dispatched == []


class _Unreachable:
    workspace_mount = "/Users/founder/proj"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> object:
        raise SandboxError("no live worker for this workspace")


def _Session_at(path: str, log: list[tuple[str, str]]) -> object:  # noqa: N802
    class _S:
        workspace_mount = path

        async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> object:
            from backend.workflow.infrastructure.sandbox import SandboxResult

            log.append((command, path))
            return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    return _S()
