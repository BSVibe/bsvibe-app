"""Reclaiming the worktrees of runs that never reached ``release``.

#736 gives a run's worktree back when its loop finishes. A run whose process was
KILLED — worker restart, machine reboot, ``kill -9`` — never runs that code, and
its checkout of the whole repo stays on the founder's disk forever with nobody
left to name it. This machine's disk filling up is an unrecoverable brick.

The founder's machine cannot decide this alone. A run that has only READ so far
has a clean, young worktree indistinguishable from an abandoned one, and
deleting it would pull the directory out from under a live run. Which runs are
still going is the SERVER's knowledge — so the machine lists what exists and the
server says what may go. Everything it says may go still has to get past git's
own refusal (:func:`orphan_reclaim_command`), which is what protects work that
exists nowhere else.

Swept at ``acquire``, the same instinct as the verification slot lease (#725):
whoever takes the resource next clears what the last holder left. No new worker,
no schedule to keep alive.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.domain.client_worktree import client_run_worktree
from backend.workflow.infrastructure.sandbox import SandboxResult
from backend.workflow.infrastructure.sandbox.client_worker_manager import (
    ClientWorkerSandboxManager,
)
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio

_REPO = "/Users/founder/proj"


class _Box:
    """Records every command, and answers the listing with a fixed porcelain."""

    def __init__(self, path: str, log: list[str], listing: str) -> None:
        self.workspace_mount = path
        self._log = log
        self._listing = listing

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self._log.append(command)
        if "worktree list" in command:
            return SandboxResult(exit_code=0, stdout=self._listing, stderr="", timed_out=False)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _manager(sf, run_id: uuid.UUID, log: list[str], listing: str) -> ClientWorkerSandboxManager:
    manager = ClientWorkerSandboxManager(
        redis=object(),
        session_factory=sf,
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        pinned_worker_id=None,
        client_workspace_dir=_REPO,
        default_timeout_s=60.0,
        run_id=run_id,
    )
    manager._session_for = lambda path: _Box(path, log, listing)  # type: ignore[attr-defined]
    return manager


async def _seed_run(sf, workspace_id: uuid.UUID, status) -> uuid.UUID:  # noqa: ANN001
    from backend.workflow.infrastructure.db import ExecutionRun

    run_id = uuid.uuid4()
    async with sf() as session:
        session.add(ExecutionRun(id=run_id, workspace_id=workspace_id, status=status, payload={}))
        await session.commit()
    return run_id


def _listing(*run_ids: uuid.UUID) -> str:
    return "\n\n".join(f"worktree {client_run_worktree(_REPO, r)}\nHEAD abc" for r in run_ids)


async def test_a_dead_runs_worktree_is_reclaimed() -> None:
    from backend.workflow.infrastructure.db import RunStatus

    async with shared_file_sessionmaker() as sf:
        log: list[str] = []
        mine, dead = uuid.uuid4(), uuid.uuid4()
        manager = _manager(sf, mine, log, _listing(mine, dead))
        await _seed_run(sf, manager._workspace_id, RunStatus.FAILED)
        await manager.acquire(uuid.uuid4(), "/server/side")

    swept = [c for c in log if "worktree remove" in c]
    assert any(str(dead)[:8] in c for c in swept), (
        f"the abandoned worktree was left on the founder's disk: {log}"
    )


async def test_a_live_runs_worktree_is_left_alone() -> None:
    """The one that matters. Removing the directory a running agent is working
    in does not lose committed work — but it does pull the ground out from under
    that run, which is a fault we would have caused."""
    from backend.workflow.infrastructure.db import RunStatus

    async with shared_file_sessionmaker() as sf:
        log: list[str] = []
        mine = uuid.uuid4()
        manager = _manager(sf, mine, log, "")
        live = await _seed_run(sf, manager._workspace_id, RunStatus.RUNNING)
        other = await _seed_run(sf, manager._workspace_id, RunStatus.OPEN)
        manager._session_for = lambda path: _Box(path, log, _listing(mine, live, other))  # type: ignore[attr-defined]
        await manager.acquire(uuid.uuid4(), "/server/side")

    swept = " ".join(c for c in log if "worktree remove" in c)
    assert str(live)[:8] not in swept, "a RUNNING run's worktree was reclaimed under it"
    assert str(other)[:8] not in swept, "an OPEN run is waiting to be picked up, not abandoned"


async def test_this_runs_own_worktree_is_never_swept() -> None:
    """It was provisioned two lines earlier and the box is about to be rooted in
    it. The run's status at this moment is whatever the caller left it as, so
    this cannot rely on the status query."""
    from backend.workflow.infrastructure.db import RunStatus

    async with shared_file_sessionmaker() as sf:
        log: list[str] = []
        mine = uuid.uuid4()
        manager = _manager(sf, mine, log, _listing(mine))
        # Deliberately terminal: a resumed / re-driven run can be in any state.
        async with sf() as session:
            from backend.workflow.infrastructure.db import ExecutionRun

            session.add(
                ExecutionRun(
                    id=mine,
                    workspace_id=manager._workspace_id,
                    status=RunStatus.FAILED,
                    payload={},
                )
            )
            await session.commit()
        await manager.acquire(uuid.uuid4(), "/server/side")

    swept = " ".join(c for c in log if "worktree remove" in c)
    assert str(mine)[:8] not in swept


async def test_a_failed_sweep_does_not_fail_the_run() -> None:
    """Best-effort by construction. ``acquire`` runs before the first agent
    turn; refusing to start a run because some other run's leftovers could not
    be tidied would trade the work for the housekeeping."""
    from backend.workflow.infrastructure.sandbox.errors import SandboxError

    class _Broken(_Box):
        async def exec(self, command: str, *, timeout_s: float, shell: bool = False):  # type: ignore[no-untyped-def]
            if "worktree list" in command:
                raise SandboxError("the machine went away")
            return await super().exec(command, timeout_s=timeout_s, shell=shell)

    async with shared_file_sessionmaker() as sf:
        log: list[str] = []
        mine = uuid.uuid4()
        manager = _manager(sf, mine, log, "")
        manager._session_for = lambda path: _Broken(path, log, "")  # type: ignore[attr-defined]

        box = await manager.acquire(uuid.uuid4(), "/server/side")

    assert box.workspace_mount == client_run_worktree(_REPO, mine)
