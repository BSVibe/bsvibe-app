"""Tests for ClientWorkerSandboxSession — the #692 in-place verify backend.

A ``client_attach`` product's source + toolchain live on the FOUNDER's own
machine, not the server. The derived verification gate still needs to RUN
commands there and read the repo's manifests, with the exit code as the verdict
(never a model's opinion). This session implements the ``SandboxSession``
Protocol by dispatching ONE ``exec`` worker task per command over the SAME
dispatch/result substrate the agent turns use (``backend.executors.dispatch``) —
no new channel to the worker.

The "worker" here is simulated the way A/2's real worker behaves: it pulls the
``action=exec`` task off its stream, runs the command in ``workspace_dir`` with a
combined stdout/stderr tail, and reports the exit code via ``record_result``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# Registering the executor tables on Base.metadata so create_all materialises them.
import backend.executors.db  # noqa: F401
from backend.executors import dispatch, service
from backend.executors.db import WorkerRow
from backend.workflow.infrastructure.sandbox import SandboxError

from ..._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


async def _make_redis() -> Any:
    try:
        import fakeredis
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:  # pragma: no cover - fakeredis is a declared dep
        pytest.skip("fakeredis not installed")
    client = fakeredis_aio.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    await client.flushdb()
    return client


async def _seed_worker(
    factory: Any,
    *,
    workspace_id: uuid.UUID,
    capability: str = "claude_code",
    heartbeat_age_s: float = 5.0,
) -> uuid.UUID:
    async with factory() as s:
        worker = WorkerRow(
            workspace_id=workspace_id,
            name="mac-mini",
            labels=[],
            capabilities=[capability],
            status="online",
            last_heartbeat=datetime.now(UTC) - timedelta(seconds=heartbeat_age_s),
            last_in_flight=0,
            token_hash=service._hash_token(uuid.uuid4().hex),
            is_active=True,
        )
        s.add(worker)
        await s.flush()
        wid = worker.id
        await s.commit()
    return wid


async def _run_one_exec_task(redis: Any, factory: Any, worker_id: uuid.UUID) -> None:
    """Simulate A/2's worker for exactly one ``exec`` task on ``worker_id``'s stream.

    Blocks (polling the stream) until a task appears, runs it in ``workspace_dir``
    with a combined stdout/stderr tail, and reports the exit code — mirroring
    ``backend/executors/worker/main.py::_handle_exec_task``.
    """
    stream = dispatch.worker_stream(worker_id)
    last_id = "0"
    for _ in range(200):  # ~10s ceiling — the command itself is trivial
        entries = await redis.xread({stream: last_id}, count=10, block=50)
        if not entries:
            continue
        for _stream, msgs in entries:
            for entry_id, fields in msgs:
                last_id = entry_id
                if fields.get("action") != "exec":
                    continue
                task_id = uuid.UUID(fields["task_id"])
                command = fields["prompt"]
                cwd = fields.get("workspace_dir") or "."
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=cwd if Path(cwd).is_dir() else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                exit_code = proc.returncode or 0
                async with factory() as s:
                    await dispatch.record_result(
                        s,
                        redis,
                        task_id=task_id,
                        success=exit_code == 0,
                        output=out.decode("utf-8", errors="replace")[-20_000:],
                        error_message=None if exit_code == 0 else f"exit {exit_code}",
                    )
                    await s.commit()
                return


def _make_session(
    *, redis: Any, factory: Any, workspace_id: uuid.UUID, workspace_path: str, worker_id: Any = None
) -> Any:
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    return ClientWorkerSandboxSession(
        redis=redis,
        session_factory=factory,
        workspace_id=workspace_id,
        executor_type="claude_code",
        workspace_path=workspace_path,
        default_timeout_s=30.0,
        pinned_worker_id=worker_id,
    )


async def test_exec_runs_command_on_worker_and_maps_zero_exit(tmp_path: Path) -> None:
    """A passing command → ``SandboxResult(exit_code=0)``. The command RAN on the
    (simulated) founder machine; the verdict is its real exit status."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        (tmp_path / "marker.txt").write_text("hi")
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        exec_coro = box.exec("test -f marker.txt", timeout_s=10.0, shell=True)
        worker_coro = _run_one_exec_task(redis, factory, wid)
        result, _ = await asyncio.gather(exec_coro, worker_coro)
        assert result.exit_code == 0
        assert result.timed_out is False
        # The command ran in the workspace, untouched by exec itself.
        assert (tmp_path / "marker.txt").exists()
    await redis.aclose()


async def test_exec_failing_command_maps_nonzero_exit(tmp_path: Path) -> None:
    """A non-zero exit is a REAL gate failure, surfaced as ``exit_code != 0`` with
    the command's output — never swallowed into a pass."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        result, _ = await asyncio.gather(
            box.exec("echo boom; exit 3", timeout_s=10.0, shell=True),
            _run_one_exec_task(redis, factory, wid),
        )
        assert result.exit_code != 0
        assert result.timed_out is False
        assert "boom" in result.stdout


async def test_read_file_returns_contents(tmp_path: Path) -> None:
    """``read_file`` is fulfilled with a capped ``head -c`` on the worker."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        data, _ = await asyncio.gather(
            box.read_file("pyproject.toml", 8192),
            _run_one_exec_task(redis, factory, wid),
        )
        assert b"name = 'x'" in data


async def test_read_file_missing_raises_sandbox_error(tmp_path: Path) -> None:
    """A missing manifest raises ``SandboxError`` (the same signal a real sandbox
    gives), so ``_read_repo_manifests`` skips it rather than false-failing."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        with pytest.raises(SandboxError):
            await asyncio.gather(
                box.read_file("nope.toml", 8192),
                _run_one_exec_task(redis, factory, wid),
            )
    await redis.aclose()


async def test_list_dir_returns_entries(tmp_path: Path) -> None:
    """``list_dir`` is fulfilled with ``ls`` on the worker, dirs suffixed ``/``
    (matching the host NoopSandboxSession's shape)."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "sub").mkdir()
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        entries, _ = await asyncio.gather(
            box.list_dir("."),
            _run_one_exec_task(redis, factory, wid),
        )
        assert "a.py" in entries
        assert "sub/" in entries


async def test_exec_no_live_worker_raises(tmp_path: Path) -> None:
    """No live worker for the workspace → ``SandboxError`` (an infra failure that
    fails CLOSED), NEVER a non-zero exit that would read as a gate FAILURE."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        # No worker seeded.
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        with pytest.raises(SandboxError):
            await box.exec("true", timeout_s=5.0, shell=True)
    await redis.aclose()


async def test_workspace_mount_is_the_user_dir(tmp_path: Path) -> None:
    """``workspace_mount`` is the founder's own directory — the verify PATH prefix
    (``{workspace_mount}/.venv/bin``) is built from it."""
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        assert box.workspace_mount == str(tmp_path)
    await redis.aclose()


async def test_manager_acquire_uses_founder_dir_not_callers_server_path(tmp_path: Path) -> None:
    """``acquire`` roots the box at the FOUNDER's dir, ignoring the caller's path.

    Live E2E 2026-08-09 (run ``27e462d5``): the real caller is
    ``agent_loop.acquire(project_id, str(workspace_dir))``, and that
    ``workspace_dir`` is the run's SERVER-side directory
    (``/app/var/runs/<run_id>``) — a path that does not exist on the founder's
    machine. Binding it made every gate command target a nonexistent dir, the
    worker fail-loud'ed ``client_attach_workspace_missing``, no manifest could be
    read, and the run settled UNTESTED as if the repo were gateless. The founder's
    dir is dispatch context (it comes from the PRODUCT), so the MANAGER owns it;
    the caller's per-run path is meaningless here and must not win."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxManager,
    )

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        mgr = ClientWorkerSandboxManager(
            redis=redis,
            session_factory=factory,
            workspace_id=workspace_id,
            executor_type="claude_code",
            pinned_worker_id=None,
            default_timeout_s=30.0,
            client_workspace_dir=str(tmp_path),
        )
        box = await mgr.acquire(uuid.uuid4(), "/app/var/runs/1a2b3c4d-server-side")
        assert box.workspace_mount == str(tmp_path)
        assert await mgr.health() is True
        await mgr.release(uuid.uuid4())
    await redis.aclose()
