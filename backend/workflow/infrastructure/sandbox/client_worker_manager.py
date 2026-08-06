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
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._executor_type = executor_type
        self._workspace_path = workspace_path
        self._default_timeout_s = default_timeout_s
        self._pinned_worker_id = pinned_worker_id

    @property
    def workspace_mount(self) -> str:
        return self._workspace_path

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
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
                self._redis, session=session, task=task, worker_id=worker_id, action="exec"
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
    workspace + executor type + the pinned founder worker). ``acquire`` binds the
    founder's workspace path — so the ``acquire(project_id, workspace_dir)`` call
    in the agent loop is unchanged when this manager is swapped in for a
    ``client_attach`` run.
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
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._executor_type = executor_type
        self._pinned_worker_id = pinned_worker_id
        self._default_timeout_s = default_timeout_s

    async def acquire(
        self, project_id: uuid.UUID, workspace_path: str
    ) -> ClientWorkerSandboxSession:
        return ClientWorkerSandboxSession(
            redis=self._redis,
            session_factory=self._session_factory,
            workspace_id=self._workspace_id,
            executor_type=self._executor_type,
            workspace_path=workspace_path,
            default_timeout_s=self._default_timeout_s,
            pinned_worker_id=self._pinned_worker_id,
        )

    async def release(self, project_id: uuid.UUID) -> None:
        return None

    async def reap_idle(self) -> None:
        return None

    async def health(self) -> bool:
        return True
