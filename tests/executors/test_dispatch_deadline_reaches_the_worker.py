"""The awaiter's deadline must travel with the task (#965).

Prod, 2026-09-15: a framing turn's ``claude`` subprocess hung. The backend
gave up after 300s — the ``frame`` caller's ``default_timeout_s`` — and failed
the run. The WORKER kept the subprocess alive and its only slot occupied for
another **eight minutes**, until a human killed the process; it stopped only
because of that kill, not because of any deadline of its own.

The two ends never agreed because the worker was never told:

* the backend's wait is **per caller** — ``frame`` 300s, ``judge`` 300s,
  ``agent_loop.act`` ``None`` → ``settings.executor_task_timeout_s`` (3600s);
* the worker applies ONE deadline to every task it ever runs
  (``ClaudeCodeExecutor``: 3600s per line, 7200s total), because the dispatch
  payload carries no deadline at all.

So for every caller except the long act turn, the worker outlives the awaiter —
and while it does, it is at capacity and stops polling, so nothing else is
dispatched to it either. The fix is not a new number; it is telling the worker
the number the awaiter is already using.

Both ends are asserted here: the payload carries it, and the worker honours it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.executors.worker.executors import ExecutionChunk

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Backend end — the dispatch payload carries the awaiter's deadline
# ---------------------------------------------------------------------------
class _CapturingRedis:
    """Records XADDs; enough of the client surface for ``dispatch_task``."""

    def __init__(self) -> None:
        self.adds: list[tuple[str, dict[str, Any]]] = []

    async def xadd(self, name: str, fields: dict[str, Any], **_kw: Any) -> str:
        self.adds.append((name, dict(fields)))
        return "1-0"


async def test_dispatch_payload_carries_the_timeout(tmp_path: Any) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base
    from backend.executors import dispatch
    from backend.executors.db import ExecutorTaskRow

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        redis = _CapturingRedis()
        worker_id = uuid.uuid4()
        async with sm() as session:
            task = ExecutorTaskRow(
                id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                executor_type="claude_code",
                prompt="p",
                system="",
                workspace_dir="/srv/run",
                status="pending",
            )
            session.add(task)
            await session.flush()
            await dispatch.dispatch_task(
                redis,
                session=session,
                task=task,
                worker_id=worker_id,
                timeout_s=300.0,
            )

        assert redis.adds, "nothing was XADDed"
        _, payload = redis.adds[0]
        assert payload.get("timeout_s") == "300.0", (
            f"the worker cannot honour a deadline it is never told: {sorted(payload)}"
        )
    finally:
        await engine.dispose()


async def test_dispatch_omits_timeout_when_the_caller_has_none() -> None:
    """Redis Streams reject ``None`` — omit the key, as ``model``/``repo_url`` do.

    A worker reading a payload without it keeps its own default, which is the
    back-compat path for an older backend.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.data import Base
    from backend.executors import dispatch
    from backend.executors.db import ExecutorTaskRow

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        redis = _CapturingRedis()
        async with sm() as session:
            task = ExecutorTaskRow(
                id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                executor_type="claude_code",
                prompt="p",
                system="",
                workspace_dir="/srv/run",
                status="pending",
            )
            session.add(task)
            await session.flush()
            await dispatch.dispatch_task(redis, session=session, task=task, worker_id=uuid.uuid4())
        _, payload = redis.adds[0]
        assert "timeout_s" not in payload
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Worker end — it gives up at that deadline and REPORTS, freeing its slot
# ---------------------------------------------------------------------------
class _HangingExecutor:
    """Stands in for the hung ``claude --print`` seen in prod: never finishes."""

    def __init__(self) -> None:
        self.cancelled = False

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        yield ExecutionChunk(delta="starting")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield ExecutionChunk(done=True)


async def test_worker_gives_up_at_the_dispatched_deadline_and_reports() -> None:
    """RED before the fix: ``handle_task`` waits on its own 3600s/7200s deadline,
    so this hangs until the test's own timeout — exactly what prod did for eight
    minutes while the backend had already failed the run."""
    from backend.executors.worker import main as worker_main
    from tests.executors.worker.test_main import _client, _task

    state: dict[str, Any] = {}
    executor = _HangingExecutor()
    async with _client(state) as client:
        await asyncio.wait_for(
            worker_main.handle_task(
                _task(timeout_s="0.3"),
                executors={"claude_code": executor},
                client=client,
                headers={"X-Worker-Token": "WORKER-TOKEN"},
                redis=None,
            ),
            timeout=15,
        )

    assert state.get("results"), (
        "the worker must REPORT the give-up, not just stop: a silent abort leaves "
        "the row `dispatched` and the awaiter waiting out its own timeout"
    )
    body = state["results"][0]
    assert body["success"] is False
    message = body.get("error_message") or ""
    # Name the deadline: an operator must be able to tell this apart from a CLI
    # error, and from the backend-side timeout that reads almost the same.
    assert "gave up" in message and "0.3" in message, body
    # The load-bearing half — the slot is actually FREED, not just abandoned.
    # Without the cancellation the subprocess keeps running (prod: eight minutes)
    # and the worker stays saturated, so it stops polling for anything else.
    assert executor.cancelled, "the hung work must be cancelled, not merely left behind"


# ---------------------------------------------------------------------------
# The wiring — a capability nothing passes is a capability that does not exist
# ---------------------------------------------------------------------------
async def test_the_adapter_dispatches_the_same_deadline_it_waits_on(monkeypatch: Any) -> None:
    """Asserted at the seam the production path crosses: whatever
    ``await_completion`` is given must be what ``dispatch_task`` was given.

    Checking merely that "a timeout was sent" would pass on a hard-coded
    default and miss exactly the per-caller mismatch this fixes — ``frame``
    waits 300s while the worker assumed 3600s.
    """
    from backend.config import get_settings
    from backend.dispatch.adapter import ExecutorAdapter, ExecutorAdapterUnavailable
    from backend.executors import dispatch as dispatch_mod
    from tests._support import memory_session
    from tests.dispatch.test_adapter import _stub_account

    seen: dict[str, Any] = {}

    async def _fake_dispatch_task(_redis: Any, **kwargs: Any) -> str:
        seen["dispatched"] = kwargs.get("timeout_s")
        return "1-0"

    async def _fake_await_completion(_redis: Any, **kwargs: Any) -> Any:
        seen["awaited"] = kwargs.get("timeout_s")
        raise dispatch_mod.TaskTimeout("stop here — both numbers are captured")

    async def _fake_cancel_task(_redis: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(dispatch_mod, "dispatch_task", _fake_dispatch_task)
    monkeypatch.setattr(dispatch_mod, "await_completion", _fake_await_completion)
    monkeypatch.setattr(dispatch_mod, "cancel_task", _fake_cancel_task)

    worker_id = uuid.uuid4()

    class _Worker:
        id = worker_id

    async def _fake_find_worker(*_a: Any, **_kw: Any) -> Any:
        return _Worker()

    monkeypatch.setattr(dispatch_mod, "find_available_worker", _fake_find_worker)

    async with memory_session() as session:
        adapter = ExecutorAdapter(
            account=_stub_account("executor", extra_params={"executor_type": "claude_code"}),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            model_account_id=uuid.uuid4(),
            session=session,
            settings=get_settings(),
            redis=_CapturingRedis(),
            timeout_s=300.0,
        )
        with pytest.raises(ExecutorAdapterUnavailable):
            await adapter.chat(system="x", messages=[{"role": "user", "content": "y"}])

    assert seen.get("dispatched") is not None, "the dispatch carried no deadline at all"
    assert seen["dispatched"] == seen["awaited"] == 300.0, (
        "the worker was told a different deadline than the one the backend waits on: "
        f"dispatched={seen.get('dispatched')} awaited={seen.get('awaited')}"
    )
