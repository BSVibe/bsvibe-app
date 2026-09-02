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
import re
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


#: Wall-clock budget for the simulated worker to see a dispatched task. Generous
#: on purpose: it bounds a HANG, it is not a performance assertion, and a tight
#: bound here only ever produces false failures.
_FAKE_WORKER_BUDGET_S = 60.0


async def _worker_then(redis: Any, factory: Any, worker_id: uuid.UUID, request: Any) -> Any:
    """Start the fake worker FIRST, let it reach its ``xread``, then issue *request*.

    ``asyncio.gather(request, worker)`` starts the request first and only yields
    to the worker when the request awaits — so the dispatch can land before the
    worker has ever polled. The stream read starts at ``"0"`` so a task that
    arrived early is still seen, which is why this raced correctly almost every
    time and then did not (CI 2026-08-25). Ordering it explicitly removes the
    question rather than re-tuning a timeout around it.
    """
    progress: dict[str, float] = {}
    worker = asyncio.create_task(_run_one_exec_task(redis, factory, worker_id, progress))
    # One event-loop turn is enough to run the worker up to its first await;
    # sleep(0) yields without adding wall-clock to every test that uses this.
    await asyncio.sleep(0)
    started = asyncio.get_running_loop().time()
    try:
        result = await request
    finally:
        # The request finished (or raised). Let the worker finish its own task —
        # it must never be left pending, which would leak into the next test.
        await worker
    # The exec side returned. If it gave up (its own timeout) there are exactly
    # two shapes left, and until now NOTHING recorded which one it was — both
    # surfaced as the product's "exec timed out … last status 'dispatched'" and
    # then as a bare ``assert None == 0`` (CI 2026-09-02, PR #873, a docs-only
    # change). The discriminator is whether this worker had finished RECORDING
    # by the time the product stopped waiting:
    #
    #   recorded_at MISSING → the runner was too slow; the product waited
    #     honestly and there was nothing to see. A HARNESS failure, and it must
    #     say so in its own voice rather than let the product take the blame.
    #   recorded_at PRESENT → the result was committed and the awaiter's polls
    #     still never saw it. That is the product-side gap ``TaskTimeout``'s
    #     docstring located at PR #828 and did not close — and the test's own
    #     assertion is then the RIGHT failure to see.
    #
    # Only the first case is claimed here. The second is deliberately left to
    # fail on the real assertion, because that one IS the product.
    if "recorded_at" not in progress:
        elapsed = asyncio.get_running_loop().time() - started
        seen_at = progress.get("seen_at")
        raise AssertionError(
            f"the exec side stopped waiting after {elapsed:.1f}s while the fake worker "
            f"had not yet recorded a result "
            f"(saw the task: {'yes, +%.1fs' % (seen_at - started) if seen_at else 'never'}). "
            "The harness/runner was too slow — this is NOT the product losing a "
            "recorded result."
        )
    return result


async def _run_one_exec_task(
    redis: Any, factory: Any, worker_id: uuid.UUID, progress: dict[str, float] | None = None
) -> None:
    """Simulate A/2's worker for exactly one ``exec`` task on ``worker_id``'s stream.

    Blocks (polling the stream) until a task appears, runs it in ``workspace_dir``
    with a combined stdout/stderr tail, and reports the exit code — mirroring
    ``backend/executors/worker/main.py::_handle_exec_task``.

    ``progress`` is stamped as this worker passes each milestone, so
    :func:`_worker_then` can tell WHICH SIDE was late when the exec times out.
    Without it the two remaining failure shapes are indistinguishable, and the
    one that reaches CI wears the product's face either way.
    """
    marks = progress if progress is not None else {}
    stream = dispatch.worker_stream(worker_id)
    last_id = "0"
    # Poll to a WALL-CLOCK deadline, not a fixed iteration count. 200 iterations
    # of ``block=50`` is only ~10s if every read returns on time — on a loaded
    # CI runner they do not, so the fake worker gave up before the task even
    # arrived and the exec side then sat out its own (much longer) timeout. The
    # failure looked like a product timeout and was really a harness with no
    # margin (CI flake, 2026-08-10). The dispatch it waits for is a DB commit +
    # XADD, so the budget must cover a slow runner, not a fast laptop.
    started = asyncio.get_running_loop().time()
    deadline = started + _FAKE_WORKER_BUDGET_S
    reads = 0
    seen: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        entries = await redis.xread({stream: last_id}, count=10, block=50)
        reads += 1
        if not entries:
            continue
        for _stream, msgs in entries:
            for entry_id, fields in msgs:
                last_id = entry_id
                if fields.get("action") != "exec":
                    continue
                seen.append(str(fields.get("action")))
                marks["seen_at"] = asyncio.get_running_loop().time()
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
                marks["recorded_at"] = asyncio.get_running_loop().time()
                return
    # The budget elapsed without an ``exec`` task ever arriving. Returning here
    # — which is what this helper used to do — is the WORST possible outcome:
    # the exec side then sits out its OWN 90 s timeout and fails with
    # ``SandboxError: list_dir '.': exit None``, which points at the product
    # code while the thing that actually failed was this harness. CI flake
    # 2026-08-25 cost a full investigation to that misattribution.
    #
    # So fail HERE, loudly, with what this loop actually observed. The numbers
    # are the whole point: a starved event loop shows few reads, a wrong stream
    # shows many reads and nothing seen, and a task on the right stream with the
    # wrong action shows up in ``seen``.
    elapsed = asyncio.get_running_loop().time() - started
    raise AssertionError(
        f"fake worker saw no exec task on {stream} in {elapsed:.1f}s "
        f"({reads} xread calls, actions seen: {seen or 'none'}). "
        "The harness failed, not the code under test."
    )


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
        result = await _worker_then(
            redis, factory, wid, box.exec("test -f marker.txt", timeout_s=10.0, shell=True)
        )
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
        result = await _worker_then(
            redis, factory, wid, box.exec("echo boom; exit 3", timeout_s=10.0, shell=True)
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
        data = await _worker_then(redis, factory, wid, box.read_file("pyproject.toml", 8192))
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
            await _worker_then(redis, factory, wid, box.read_file("nope.toml", 8192))
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
        entries = await _worker_then(redis, factory, wid, box.list_dir("."))
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


# ── the harness must accuse ITSELF, not the code under test ──────────────────
#
# CI 2026-08-25: ``_run_one_exec_task`` burned its 60 s budget without ever
# seeing a task and then simply returned. The exec side sat out its own 90 s
# timeout and the test died as
# ``SandboxError: list_dir '.': exit None`` — pointing squarely at
# ``client_worker_manager``, which had done nothing wrong. A full investigation
# went into product code before the harness turned out to be the failing party.


async def test_fake_worker_that_sees_no_task_accuses_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the fake worker at a stream nobody dispatches to. It must fail
    LOUDLY as a harness failure — not return, letting the exec side time out
    and report a product error 90 seconds later."""
    import tests.supervisor.sandbox.test_client_worker_manager as mod

    monkeypatch.setattr(mod, "_FAKE_WORKER_BUDGET_S", 0.3)
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        with pytest.raises(AssertionError) as exc:
            await _run_one_exec_task(redis, factory, uuid.uuid4())
    await redis.aclose()

    message = str(exc.value)
    # It names itself as the failing party…
    assert "harness failed, not the code under test" in message
    # …and reports what it actually observed, which is what a CI-only flake
    # needs: a starved loop shows few reads, a wrong stream shows many with
    # nothing seen, a wrong action shows up under "actions seen".
    assert "xread calls" in message
    assert "actions seen" in message


async def test_the_worker_is_never_left_pending_when_the_request_raises(
    tmp_path: Path,
) -> None:
    """``_worker_then`` awaits the worker even when the request raises.

    ``asyncio.gather`` does not: it propagates the first exception and leaves
    the sibling running, so a failing test could leak a 60-second coroutine into
    whatever ran next.
    """
    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        wid = await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )
        with pytest.raises(SandboxError):
            await _worker_then(redis, factory, wid, box.read_file("nope.toml", 8192))
        # Nothing of ours is still scheduled.
        others = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and "_run_one_exec_task" in repr(t)
        ]
        assert others == []
    await redis.aclose()


# --------------------------------------------------------------------------
# The diagnosis has to reach the place the failure is READ
# --------------------------------------------------------------------------
#
# `TaskTimeout` now carries polls / last_status / elapsed (#821 follow-up). If
# the sandbox layer drops them, a CI failure still reads "exec timed out after
# 70s" and the investigation starts from zero again — which is exactly what
# happened on 2026-08-26.


async def test_a_timed_out_exec_carries_the_awaiters_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SandboxResult's stderr is what a failing assertion prints. It must
    name what the awaiter saw, not just that time ran out."""
    from backend.executors import dispatch as dispatch_mod

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )

        async def _always_timeout(*_a: Any, **kw: Any) -> Any:
            raise dispatch_mod.TaskTimeout(
                "forced",
                task_id=kw.get("task_id"),
                polls=3,
                last_status="dispatched",
                elapsed_s=7.5,
            )

        monkeypatch.setattr(dispatch_mod, "await_completion", _always_timeout)
        result = await box.exec("true", timeout_s=1.0, shell=True)

    assert result.timed_out is True
    assert "3 polls" in result.stderr
    assert "dispatched" in result.stderr
    await redis.aclose()


# --------------------------------------------------------------------------
# A timeout that used exactly its budget must not READ as an overrun
# --------------------------------------------------------------------------
#
# The awaiter's budget is ``timeout_s + _AWAIT_SLACK_S`` (10 + 60 = 70 for the
# tests above), but the message printed ``timeout_s`` — so a healthy, full-budget
# timeout rendered as "after 10.0s (36 polls in 70.0s)". Reproduced 2026-09-01 on
# an IDLE laptop, byte-identical to the CI string: the apparent "7x budget
# overrun" is a constant of ``_AWAIT_SLACK_S``, not a load signal. It sent one
# investigation to "starved CI runner" and the real shape — ``polls`` NEAR
# ``timeout_s / _AWAIT_POLL_INTERVAL_S`` with ``last_status='dispatched'`` — is
# the one ``TaskTimeout`` documents as "the worker never reported".
#
# The proposition is not a spelling: **the budget the message says ran out must
# be at least the elapsed time it reports.** Any message that fails that invites
# the overrun misreading again.


async def test_a_full_budget_timeout_does_not_read_as_a_budget_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.executors import dispatch as dispatch_mod
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        _AWAIT_SLACK_S,
        _AWAITER_TIMEOUT_PREFIX,
    )

    timed_out_after_re = re.compile(rf"{re.escape(_AWAITER_TIMEOUT_PREFIX)}([\d.]+)s")

    command_budget_s = 10.0
    # What a healthy awaiter that never hears back actually spends.
    awaiter_budget_s = command_budget_s + _AWAIT_SLACK_S

    workspace_id = uuid.uuid4()
    redis = await _make_redis()
    async with shared_file_sessionmaker() as factory:
        await _seed_worker(factory, workspace_id=workspace_id)
        box = _make_session(
            redis=redis, factory=factory, workspace_id=workspace_id, workspace_path=str(tmp_path)
        )

        async def _timeout_at_full_budget(*_a: Any, **kw: Any) -> Any:
            raise dispatch_mod.TaskTimeout(
                "forced",
                task_id=kw.get("task_id"),
                polls=36,
                last_status="dispatched",
                elapsed_s=awaiter_budget_s,
            )

        monkeypatch.setattr(dispatch_mod, "await_completion", _timeout_at_full_budget)
        result = await box.exec("true", timeout_s=command_budget_s, shell=True)

    match = timed_out_after_re.search(result.stderr)
    assert match is not None, f"no budget stated in {result.stderr!r}"
    stated_budget_s = float(match.group(1))
    assert stated_budget_s >= awaiter_budget_s, (
        f"the message says it timed out after {stated_budget_s}s but reports "
        f"{awaiter_budget_s}s elapsed — a full-budget timeout reads as a "
        f"{awaiter_budget_s / stated_budget_s:.0f}x overrun: {result.stderr!r}"
    )
    await redis.aclose()


# --------------------------------------------------------------------------
# The producer's timeout wording and the consumer's ``startswith`` check live
# in different files. Nothing enforces they still agree.
# --------------------------------------------------------------------------
#
# The worker (``backend/executors/worker/main.py::_handle_exec_task``) reports a
# command timeout with an ``error_message`` built by ``_exec_timeout_error``.
# ``_map_result`` here recognises a timeout by ``err.startswith("exec timed
# out")``. If the worker's wording drifts, ``_map_result`` falls through past
# ``_EXIT_RE`` (no ``"exit N"`` in the message) to ``exit_code = 1`` — an infra
# timeout silently reads as a gate FAILURE, the exact inversion
# ``test_exec_no_live_worker_raises`` above says must never happen.
#
# This does not retype the worker's literal: it imports the SAME function the
# worker calls and feeds its OUTPUT straight into ``_map_result``, so a change
# to either side that breaks the coupling breaks this test too.


async def test_map_result_reads_the_workers_actual_timeout_message() -> None:
    """Feed the worker's OWN timeout message into ``_map_result`` and require
    ``timed_out=True, exit_code=None`` — the contract, not a copy of the string."""
    from types import SimpleNamespace

    from backend.executors.worker.main import _exec_timeout_error
    from backend.workflow.infrastructure.sandbox.client_worker_manager import _map_result

    worker_message = _exec_timeout_error("pytest -q")
    row = SimpleNamespace(status="failed", error_message=worker_message, output="")

    result = _map_result(row)

    assert result.timed_out is True
    assert result.exit_code is None
