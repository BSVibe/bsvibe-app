"""Client-attach sandbox backend — #692 in-place verify.

A ``client_attach`` product's source and toolchain live on the FOUNDER's own
machine, never the server (the privacy contract). Yet the derived verification
gate must still RUN commands there and read the repo's declaration files, with
the command's exit code as the verdict — never a model's opinion. That is
exactly the honesty the sandbox gate rests on, and it holds unchanged when the
command runs on the user's machine instead of a server DinD container.

``ClientWorkerSandboxSession`` implements the ``SandboxSession`` Protocol by
dispatching ONE ``action="exec"`` worker task per command over the SAME
dispatch/result substrate the agent turns already use
(:mod:`backend.executors.dispatch`) — no second channel to the worker. The
worker (``backend/executors/worker/main.py::_handle_exec_task``) runs the command
in the founder's ``workspace_dir`` and reports its exit code; this session maps
that back to a :class:`SandboxResult`. ``read_file`` / ``list_dir`` are fulfilled
with ``head``/``ls`` commands over the same channel.

Only command + exit code / output cross the wire — the source files never come
to the server (same exposure level as a server-side sandbox verify, whose gate
output can already echo code).
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.infrastructure.sandbox.errors import SandboxError
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult

logger = structlog.get_logger(__name__)

#: Extra head-room added to a command's own timeout when waiting for the worker
#: to REPORT — so a command that runs to its own ceiling and then reports a
#: timeout failure is still received, rather than the awaiter giving up first.
_AWAIT_SLACK_S = 60.0

#: Bound for the tiny ``head``/``ls`` file-op commands (they never run user code).
_FILE_OP_TIMEOUT_S = 30.0

#: Provisioning is a `git worktree add` — a local checkout, not a build. Its own
#: budget so a wedged git cannot eat the run's whole clock.
_WORKTREE_TIMEOUT_S = 120.0

_EXIT_RE = re.compile(r"exit (\d+)")


def _map_result(row: Any) -> SandboxResult:
    """Map a terminal :class:`ExecutorTaskRow` back to a :class:`SandboxResult`.

    The worker reports a combined stdout/stderr tail in ``output`` and encodes
    the exit code in ``error_message`` (``"exit N"``); a timeout is reported as a
    failure whose message starts with ``"exec timed out"``. Success is a clean
    ``exit 0``.
    """
    err = row.error_message or ""
    output = row.output or ""
    if err.startswith("exec timed out"):
        return SandboxResult(exit_code=None, stdout=output, stderr=err, timed_out=True)
    if row.status == "done":
        return SandboxResult(exit_code=0, stdout=output, stderr="", timed_out=False)
    # A real non-zero exit — the diagnostic text already rides in ``output``.
    match = _EXIT_RE.search(err)
    exit_code = int(match.group(1)) if match else 1
    return SandboxResult(exit_code=exit_code, stdout=output, stderr="", timed_out=False)


class ClientWorkerSandboxSession:
    """A ``SandboxSession`` rooted at the founder's own workspace on the worker."""

    #: #692 — this box executes on the FOUNDER's machine, where the product's
    #: source and toolchain actually are. The drive loop reads this to decide it
    #: can run the repo's own derived gate IN PLACE (and take its exit codes as a
    #: real proof) instead of settling UNTESTED for want of a server-side copy.
    runs_in_place = True

    #: This box runs in the FOUNDER's own working directory, so the server does
    #: NOT materialise a venv there — ``uv sync`` would be an unasked-for
    #: mutation of their tree, and their toolchain is already set up (they work
    #: there). ``ensure_sandbox_ready`` honours this and skips without
    #: dispatching anything; gate commands then run bare in their environment.
    provisions_venv = False

    def __init__(
        self,
        *,
        redis: Any,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: uuid.UUID,
        executor_type: str,
        workspace_path: str,
        default_timeout_s: float,
        pinned_worker_id: uuid.UUID | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._executor_type = executor_type
        self._workspace_path = workspace_path
        self._default_timeout_s = default_timeout_s
        self._pinned_worker_id = pinned_worker_id
        # The product's DECLARED secrets. The box carries them so that every command it
        # runs has them — the agent's ``shell_exec`` and the derived gate's commands
        # alike. Attaching them at each call site instead would be a second source, and
        # the two would drift toward whichever is tested less.
        #
        # This matters here specifically because a client_attach run has no container:
        # its stack plan is ``None`` (``StackNotApplicable``), so commands run DIRECTLY
        # on this box and never touch the container path that puts secrets on the boot
        # command only. Measured 2026-08-24: the run's git worktree holds no ``.env``
        # (gitignored, so ``git worktree`` does not materialise it) and every exec went
        # out with ``env_names=[]`` — the founder's own checkout produced a real report
        # from the same command that produced "API 키가 비어 있어요" in the worktree.
        self._env: dict[str, str] = dict(env or {})

    @property
    def workspace_mount(self) -> str:
        return self._workspace_path

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        """Run ONE command on the founder's machine and map its exit code.

        ``shell`` is accepted for Protocol parity but has no effect here — the
        worker always runs the command through a shell in ``workspace_dir`` (the
        derived gate's commands are shell strings). No live worker for the
        workspace raises :class:`SandboxError`: an infra failure that fails
        CLOSED, never a non-zero exit that would masquerade as a gate failure.
        """
        from backend.executors import dispatch  # noqa: PLC0415

        async with self._session_factory() as session:
            worker = await dispatch.find_available_worker(
                session,
                workspace_id=self._workspace_id,
                executor_type=self._executor_type,
                pinned_worker_id=self._pinned_worker_id,
            )
            if worker is None:
                raise SandboxError(
                    "no live client worker for this workspace — the founder's "
                    "machine is not connected, so the gate cannot run"
                )
            worker_id = worker.id
            task = await dispatch.create_task(
                session,
                workspace_id=self._workspace_id,
                executor_type=self._executor_type,
                prompt=command,
                workspace_dir=self._workspace_path,
                execution_target="client_attach",
            )
            task_id = task.id
            await dispatch.dispatch_task(
                self._redis,
                session=session,
                task=task,
                worker_id=worker_id,
                action="exec",
                # The caller's own env WINS: the verification stack passes the boot
                # command exactly what it means to boot with, and a product default
                # must not quietly override that intent.
                env={**self._env, **(env or {})} or None,
            )
            # Commit before awaiting — the worker reports on a SEPARATE session
            # over HTTP; under PG READ COMMITTED an uncommitted row is invisible
            # to it, so it could never flip the task terminal.
            await session.commit()

            try:
                completed = await dispatch.await_completion(
                    self._redis,
                    session=session,
                    task_id=task_id,
                    timeout_s=timeout_s + _AWAIT_SLACK_S,
                    session_factory=self._session_factory,
                )
            except dispatch.TaskTimeout:
                # Signal the worker to stop the now-abandoned command, then report
                # the honest timeout (never a silent pass).
                await dispatch.cancel_task(self._redis, worker_id=worker_id, task_id=task_id)
                logger.info(
                    "client_worker_exec_timeout",
                    workspace_id=str(self._workspace_id),
                    worker_id=str(worker_id),
                    task_id=str(task_id),
                )
                return SandboxResult(
                    exit_code=None,
                    stdout="",
                    stderr=f"exec timed out after {timeout_s}s",
                    timed_out=True,
                )
        return _map_result(completed)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        """Read a capped prefix of a workspace file via ``head -c`` on the worker.

        A missing / unreadable file exits non-zero → :class:`SandboxError`, the
        same signal a real sandbox gives, so ``_read_repo_manifests`` skips it
        rather than false-failing.
        """
        cmd = f"head -c {int(max_bytes)} -- {shlex.quote(rel_path)}"
        res = await self.exec(cmd, timeout_s=_FILE_OP_TIMEOUT_S, shell=True)
        if res.timed_out or res.exit_code != 0:
            raise SandboxError(f"read_file {rel_path!r}: exit {res.exit_code}")
        return res.stdout.encode("utf-8", errors="replace")[:max_bytes]

    async def write_file(self, rel_path: str, content: bytes) -> None:
        """Not supported for client_attach — the founder's OWN tools own their
        working tree, and the server never writes to it (the privacy contract).
        The verify path never calls this; it exists only for Protocol parity."""
        raise SandboxError(
            "write_file is not supported for a client_attach workspace — the "
            "server does not write to the founder's own working tree"
        )

    async def list_dir(self, rel_path: str) -> list[str]:
        """List a workspace directory via ``ls`` on the worker (dirs suffixed
        ``/``, matching the host ``NoopSandboxSession`` shape)."""
        cmd = f"ls -1Ap -- {shlex.quote(rel_path)}"
        res = await self.exec(cmd, timeout_s=_FILE_OP_TIMEOUT_S, shell=True)
        if res.timed_out or res.exit_code != 0:
            raise SandboxError(f"list_dir {rel_path!r}: exit {res.exit_code}")
        return sorted(line for line in res.stdout.splitlines() if line)


class ClientWorkerSandboxManager:
    """Per-run manager that hands back a :class:`ClientWorkerSandboxSession`.

    Constructed with the run's dispatch context (redis + session factory +
    workspace + executor type + the pinned founder worker) AND the founder's own
    workspace directory, which comes from the product's ``client_workspace_path``
    — the only path that exists on that machine.

    ``run_id`` gives this run its OWN git worktree under that directory rather
    than letting it edit the founder's checkout. Editing in place left three
    marks, all observed: uncommitted work piling up unattributably, two runs
    unable to proceed at once, and the founder's own work-in-progress inside the
    blast radius of anything that commits. ``None`` keeps the old in-place
    behaviour for a caller that has no run to name.
    """

    def __init__(
        self,
        *,
        redis: Any,
        session_factory: async_sessionmaker[AsyncSession],
        workspace_id: uuid.UUID,
        executor_type: str,
        pinned_worker_id: uuid.UUID | None,
        default_timeout_s: float,
        client_workspace_dir: str,
        run_id: uuid.UUID | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env: dict[str, str] = dict(env or {})
        self._redis = redis
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._executor_type = executor_type
        self._pinned_worker_id = pinned_worker_id
        self._default_timeout_s = default_timeout_s
        self._client_workspace_dir = client_workspace_dir
        self._run_id = run_id

    def _session_for(self, workspace_path: str) -> ClientWorkerSandboxSession:
        return ClientWorkerSandboxSession(
            redis=self._redis,
            session_factory=self._session_factory,
            workspace_id=self._workspace_id,
            executor_type=self._executor_type,
            workspace_path=workspace_path,
            default_timeout_s=self._default_timeout_s,
            pinned_worker_id=self._pinned_worker_id,
            env=self._env,
        )

    def attach(self) -> ClientWorkerSandboxSession:
        """The run's box as it ALREADY stands — no provisioning, no commands sent.

        :meth:`acquire` belongs to the run's lifecycle: it runs once, before the drive loop
        dispatches the first agent turn, and it dispatches two exec tasks to the founder's
        machine (provision this run's worktree, sweep the orphans of runs that were killed).

        The MCP work-tool transport is a different caller with a different need. It resolves
        the box **on every tool call**, and by the time it does, the worktree exists. Calling
        ``acquire`` there charged every ``file_read`` two worker round-trips and — worse —
        ran the orphan sweep per tool call instead of once per run, multiplying the window in
        which it can reclaim a worktree from a run that is only just starting.

        So: the lifecycle provisions, the transport attaches. (The server-side backend draws
        the same line through its process-wide manager cache — see
        ``tests/mcp/test_sandbox_reuse.py``, the E1 defect.)
        """
        if self._run_id is None:
            return self._session_for(self._client_workspace_dir)
        from backend.workflow.domain.client_worktree import (  # noqa: PLC0415
            client_run_worktree,
        )

        return self._session_for(client_run_worktree(self._client_workspace_dir, self._run_id))

    async def acquire(
        self, project_id: uuid.UUID, workspace_path: str
    ) -> ClientWorkerSandboxSession:
        """Root the box at the FOUNDER's directory, ignoring ``workspace_path``.

        The ``SandboxManager`` Protocol hands in the run's own workspace, and for
        every server-side backend that is the right answer. For client_attach it
        is not: that argument is the run's SERVER-side dir
        (``/app/var/runs/<run_id>``, see ``agent_loop``), which does not exist on
        the founder's machine. Honouring it made every gate command fail with
        ``client_attach_workspace_missing``, so no manifest could be read and the
        run settled UNTESTED as though the repo had no gate at all (live E2E
        2026-08-09, run ``27e462d5``). WHERE this run executes is dispatch
        context, decided by the product — so it is the manager's to supply.

        With a ``run_id``, the box is rooted in this run's OWN worktree instead,
        provisioned here. Here specifically because ``acquire`` already runs
        BEFORE the drive loop dispatches the first agent turn, and that turn's
        CLI needs the directory to exist — the agent's dispatch derives the same
        path independently rather than being handed it.

        Provisioning failure raises: a run that silently fell back to the
        founder's checkout would be editing their tree while every record says
        otherwise, which is worse than not running.
        """
        if self._run_id is None:
            return self._session_for(self._client_workspace_dir)

        from backend.workflow.domain.client_worktree import (  # noqa: PLC0415
            client_run_worktree,
            worktree_provision_command,
        )

        # Provisioning runs in the REPO: the worktree does not exist yet.
        repo_box = self._session_for(self._client_workspace_dir)
        command = worktree_provision_command(self._client_workspace_dir, self._run_id)
        result = await repo_box.exec(command, timeout_s=_WORKTREE_TIMEOUT_S, shell=True)
        if result.timed_out or result.exit_code != 0:
            detail = "\n".join(o for o in (result.stdout, result.stderr) if o)[-500:]
            raise SandboxError(f"could not provision this run's worktree: {detail}")
        # Whoever takes the resource next clears what the last holder left —
        # the same instinct as the verification slot lease (#725), and the
        # reason no reaper worker is needed. Best-effort: this run's own
        # worktree is ready, and housekeeping must not cost it its start.
        await self._sweep_orphan_worktrees(repo_box, self._run_id)
        return self._session_for(client_run_worktree(self._client_workspace_dir, self._run_id))

    async def _sweep_orphan_worktrees(self, repo_box: Any, run_id: uuid.UUID) -> None:
        """Give back the worktrees of runs that never reached ``release``.

        #736 reclaims a run's worktree when its loop finishes. A run whose
        process was KILLED — worker restart, reboot, ``kill -9`` — never gets
        there, and its checkout of the whole repo stays on the founder's disk
        with nobody left to name it. A full disk on this machine is not a
        degradation but an unrecoverable brick.

        **The decision is the server's, the listing is the machine's.** The
        founder's machine cannot tell an abandoned worktree from a live run's:
        a run that has only READ so far has a clean, young directory that looks
        identical to one left by a run that died an hour ago. Which runs are
        still going is knowledge only this side has. So the machine says what
        exists and the server says what may go — and everything it says may go
        still has to get past git's own refusal, which is what protects work
        that exists nowhere else.
        """
        from backend.workflow.domain.client_worktree import (  # noqa: PLC0415
            parse_worktree_shorts,
            run_short,
            worktree_list_command,
        )

        try:
            listed = await repo_box.exec(
                worktree_list_command(self._client_workspace_dir),
                timeout_s=_WORKTREE_TIMEOUT_S,
                shell=True,
            )
            if listed.timed_out or listed.exit_code != 0:
                return
            shorts = parse_worktree_shorts(listed.stdout, self._client_workspace_dir)
            if not shorts:
                return
            # This run's own is excluded by name, not by status: it was
            # provisioned two lines ago and the box is about to be rooted in it,
            # while a resumed or re-driven run can be in any status at all.
            keep = await self._live_run_shorts() | {run_short(run_id)}
            for short in shorts:
                if short in keep:
                    continue
                await self._reclaim_orphan(repo_box, short)
        except Exception:  # noqa: BLE001 — housekeeping never fails a run's start
            logger.warning("client_attach_worktree_sweep_failed", run_id=str(run_id), exc_info=True)

    async def _reclaim_orphan(self, repo_box: Any, short: str) -> None:
        from backend.workflow.domain.client_worktree import (  # noqa: PLC0415
            orphan_reclaim_command,
            worktree_path_for_short,
        )

        path = worktree_path_for_short(self._client_workspace_dir, short)
        result = await repo_box.exec(
            orphan_reclaim_command(self._client_workspace_dir, path),
            timeout_s=_WORKTREE_TIMEOUT_S,
            shell=True,
        )
        if result.timed_out or result.exit_code != 0:
            # Held (it still has uncommitted work) or unreadable. Named, not
            # retried: a tree the sweep cannot take is one the founder may want.
            logger.info(
                "client_attach_orphan_worktree_kept",
                run_id=str(self._run_id),
                orphan=short,
                exit_code=result.exit_code,
            )
            return
        logger.info(
            "client_attach_orphan_worktree_reclaimed", run_id=str(self._run_id), orphan=short
        )

    async def _live_run_shorts(self) -> set[str]:
        """Short ids of runs that could still be executing in this workspace.

        ``OPEN`` counts: it is both "waiting to be picked up" and the state a
        paused run is resumed from. Everything else — ``REVIEW_READY`` included
        — means the loop is over, and #736 already reclaimed that worktree on
        the way out; seeing one still there is precisely the evidence that this
        run never reached ``release``.

        Workspace-scoped rather than product-scoped, deliberately: a superset
        keeps MORE worktrees, and erring toward keeping is the cheap mistake.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from backend.workflow.infrastructure.db import ExecutionRun, RunStatus  # noqa: PLC0415

        async with self._session_factory() as session:
            rows = await session.execute(
                select(ExecutionRun.id).where(
                    ExecutionRun.workspace_id == self._workspace_id,
                    ExecutionRun.status.in_((RunStatus.OPEN, RunStatus.RUNNING)),
                )
            )
        return {str(run_id)[:8] for run_id in rows.scalars()}

    async def release(self, project_id: uuid.UUID) -> None:
        """Give this run's worktree back to the founder's disk.

        Here and not at the verified settle, because ``agent_loop`` calls this
        in a ``finally``: it is the one seam EVERY terminal path crosses —
        proved, refuted, cancelled, crashed. Hanging the reaper off the happy
        path would reclaim nothing from the runs that end badly, which are
        exactly the ones that leave debris (#734 makes a full checkout per run
        and, until now, nothing ever removed one).

        Never forces, so a tree still holding uncommitted work is KEPT — see
        :func:`worktree_reclaim_command`. That outcome is reported as itself,
        not as a failure: it is the founder's work sitting there, and they need
        to be able to find it.

        Nothing here raises. This runs inside that ``finally``, where an
        exception would replace whatever the run was about to report — turning
        a proved run into a crash over a directory. Reclaiming disk is never
        worth that, so every failure is recorded and swallowed.
        """
        if self._run_id is None:
            # The box was rooted at the founder's OWN checkout. There is no
            # worktree of ours here, and reclaiming would mean their directory.
            return None

        from backend.workflow.domain.client_worktree import (  # noqa: PLC0415
            RECLAIM_HELD,
            client_run_worktree,
            worktree_reclaim_command,
        )

        path = client_run_worktree(self._client_workspace_dir, self._run_id)
        # In the REPO: you cannot stand in the directory you are removing.
        repo_box = self._session_for(self._client_workspace_dir)
        command = worktree_reclaim_command(self._client_workspace_dir, self._run_id)
        try:
            result = await repo_box.exec(command, timeout_s=_WORKTREE_TIMEOUT_S, shell=True)
        except Exception as exc:  # noqa: BLE001 — an unreachable machine must not fail the run
            logger.warning(
                "client_attach_worktree_reclaim_unreachable",
                run_id=str(self._run_id),
                path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

        if result.timed_out or result.exit_code == RECLAIM_HELD:
            # Not a fault: the tree holds work that exists nowhere else, so it
            # stays. Logged at warning because it is disk the founder now owns
            # deliberately, and work they may be looking for.
            logger.warning(
                "client_attach_worktree_held",
                run_id=str(self._run_id),
                path=path,
                reason="timeout" if result.timed_out else "uncommitted_work",
            )
        elif result.exit_code != 0:
            detail = "\n".join(o for o in (result.stdout, result.stderr) if o)[-500:]
            logger.warning(
                "client_attach_worktree_reclaim_failed",
                run_id=str(self._run_id),
                path=path,
                exit_code=result.exit_code,
                detail=detail,
            )
        else:
            logger.info("client_attach_worktree_reclaimed", run_id=str(self._run_id), path=path)
        return None

    async def reap_idle(self) -> None:
        return None

    async def health(self) -> bool:
        return True
