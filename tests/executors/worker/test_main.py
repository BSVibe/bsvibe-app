"""Tests for the worker poll loop (Lift 3).

NO real backend: an ``httpx.MockTransport`` answers register / heartbeat /
poll / result so the full register -> poll -> execute -> result path is
exercised in-process. The executor is faked (a tiny stub yielding one delta +
done) so no real ``claude`` runs. Determinism: the loop runs a single tick
(``run_once``) or stops after N iterations via an injected stop event — no real
sleeps gate the assertions.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from backend.executors.worker import main as worker_main
from backend.executors.worker.config import WorkerSettings
from backend.executors.worker.executors import ExecutionChunk


class _StubExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def supported_task_types(self) -> list[str]:
        return ["coding"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        yield ExecutionChunk(delta=f"ran:{prompt}")
        if self._fail:
            yield ExecutionChunk(done=True, error="exploded")
        else:
            yield ExecutionChunk(done=True)


class _WorkspaceCapturingExecutor:
    """Records the ``workspace_dir`` it was handed (and whether it existed)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.seen_workspace: str | None = None
        self.workspace_existed: bool | None = None

    def supported_task_types(self) -> list[str]:
        return ["coding"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        self.seen_workspace = context.get("workspace_dir")
        self.workspace_existed = bool(self.seen_workspace and os.path.isdir(self.seen_workspace))
        yield ExecutionChunk(delta=f"ran:{prompt}")
        if self._fail:
            raise RuntimeError("executor blew up mid-stream")
        yield ExecutionChunk(done=True)


def _mock_transport(state: dict[str, Any]) -> httpx.MockTransport:
    """Build a MockTransport recording calls + serving the worker endpoints."""

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        state.setdefault("calls", []).append((request.method, path))
        if path == "/api/v1/workers/register":
            state["register_headers"] = dict(request.headers)
            return httpx.Response(201, json={"id": str(uuid.uuid4()), "token": "WORKER-TOKEN"})
        if path == "/api/v1/workers/heartbeat":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/v1/workers/poll":
            tasks = state.get("poll_queue", [])
            state["poll_queue"] = []  # drain once
            return httpx.Response(200, json=tasks)
        if path == "/api/v1/workers/result":
            state.setdefault("results", []).append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)  # pragma: no cover

    return httpx.MockTransport(_handler)


def _client(state: dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_mock_transport(state),
        base_url="http://test",
    )


def _settings(**overrides: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "server_url": "http://test",
        "token": "WORKER-TOKEN",
        "install_token": "",
        "name": "test-worker",
        "redis_url": "",
        "poll_interval_seconds": 0,
        "max_parallel_tasks": 3,
        "poll_batch_max": 5,
    }
    base.update(overrides)
    return WorkerSettings(**base)


def _task(**overrides: Any) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    base = {
        "task_id": task_id,
        "executor_type": "claude_code",
        "prompt": "build me a thing",
        "system": "",
        "workspace_dir": ".",
        "stream_channel": f"task:{task_id}:stream",
        "done_channel": f"task:{task_id}:done",
        "action": "execute",
        "dispatched_at": "2026-05-24T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# ── Registration ─────────────────────────────────────────────────────────────


async def test_register_returns_token_and_sends_install_header() -> None:
    state: dict[str, Any] = {}
    async with _client(state) as client:
        token = await worker_main.register(
            client,
            name="w1",
            install_token="INSTALL-XYZ",
            capabilities=["claude_code"],
        )
    assert token == "WORKER-TOKEN"
    assert state["register_headers"]["x-install-token"] == "INSTALL-XYZ"


async def test_register_requires_install_token() -> None:
    state: dict[str, Any] = {}
    async with _client(state) as client:
        with pytest.raises(ValueError, match="install_token"):
            await worker_main.register(client, name="w1", install_token="", capabilities=[])


# ── Single task handling ─────────────────────────────────────────────────────


async def test_handle_task_posts_result_with_collected_output() -> None:
    state: dict[str, Any] = {}
    task = _task(prompt="hello")
    async with _client(state) as client:
        await worker_main.handle_task(
            task,
            executors={"claude_code": _StubExecutor()},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )
    assert len(state["results"]) == 1
    body = state["results"][0]
    assert body["task_id"] == task["task_id"]
    assert body["success"] is True
    assert body["output"] == "ran:hello"
    assert body["error_message"] is None


async def test_handle_task_runs_in_local_temp_dir_not_foreign_path() -> None:
    # The backend dispatches the run with ITS container path, which does not
    # exist on this (remote) worker. The worker must create its own local
    # working dir and run the executor there — never chdir into the foreign path.
    foreign = "/app/var/runs/d686dc1e-does-not-exist-here"
    assert not os.path.isdir(foreign)
    executor = _WorkspaceCapturingExecutor()
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(workspace_dir=foreign),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )
    # The executor ran in a real, existing local directory — not the foreign one.
    assert executor.seen_workspace is not None
    assert executor.seen_workspace != foreign
    assert executor.workspace_existed is True
    # Result still posted with the collected output.
    assert state["results"][0]["success"] is True


async def test_handle_task_cleans_up_local_temp_dir_after_success() -> None:
    executor = _WorkspaceCapturingExecutor()
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(workspace_dir="/app/var/runs/foreign"),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )
    # The temp dir the executor saw is gone (cleaned in finally).
    assert executor.seen_workspace is not None
    assert not os.path.exists(executor.seen_workspace)


async def test_handle_task_cleans_up_local_temp_dir_on_executor_error() -> None:
    # Even when the executor raises mid-stream, the worker's local temp dir must
    # be removed (cleanup lives in a finally, not only the happy path).
    executor = _WorkspaceCapturingExecutor(fail=True)
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(workspace_dir="/app/var/runs/foreign"),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )
    assert executor.seen_workspace is not None
    assert not os.path.exists(executor.seen_workspace)
    # The error was reported as a failed result, not a crash.
    assert state["results"][0]["success"] is False


async def test_handle_task_reports_failure_on_error_chunk() -> None:
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(),
            executors={"claude_code": _StubExecutor(fail=True)},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
        )
    body = state["results"][0]
    assert body["success"] is False
    assert body["error_message"] == "exploded"


async def test_handle_task_publishes_stream_and_done_when_redis(monkeypatch: Any) -> None:
    published: list[tuple[str, dict[str, Any]]] = []

    class _FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            published.append((channel, json.loads(payload)))

    state: dict[str, Any] = {}
    task = _task(prompt="hi")
    async with _client(state) as client:
        await worker_main.handle_task(
            task,
            executors={"claude_code": _StubExecutor()},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=_FakeRedis(),
        )
    channels = [c for c, _ in published]
    assert task["stream_channel"] in channels
    assert task["done_channel"] in channels


# ── One poll-loop tick ───────────────────────────────────────────────────────


async def test_run_once_polls_and_executes(monkeypatch: Any) -> None:
    state: dict[str, Any] = {"poll_queue": [_task(prompt="abc")]}
    monkeypatch.setattr(worker_main, "select_executor", lambda _t: _StubExecutor())

    async with _client(state) as client:
        in_flight = await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=set(),
        )
        # Drain the spawned task(s).
        for t in in_flight:
            await t

    methods = [p for _, p in state["calls"]]
    assert "/api/v1/workers/heartbeat" in methods
    assert "/api/v1/workers/poll" in methods
    assert len(state["results"]) == 1
    assert state["results"][0]["output"] == "ran:abc"


async def test_run_once_empty_poll_no_results() -> None:
    state: dict[str, Any] = {"poll_queue": []}
    async with _client(state) as client:
        in_flight = await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={"claude_code": _StubExecutor()},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=set(),
        )
    assert in_flight == set()
    assert state.get("results") is None


async def test_run_once_respects_capacity() -> None:
    # Already at capacity → no poll call, just a heartbeat.
    state: dict[str, Any] = {"poll_queue": [_task()]}

    class _NeverDone:
        def done(self) -> bool:
            return False

    in_flight = {_NeverDone(), _NeverDone(), _NeverDone()}  # == max_parallel_tasks
    async with _client(state) as client:
        result = await worker_main.run_once(
            client=client,
            settings=_settings(max_parallel_tasks=3),
            executors={"claude_code": _StubExecutor()},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=in_flight,  # type: ignore[arg-type]
        )
    methods = [p for _, p in state["calls"]]
    assert "/api/v1/workers/poll" not in methods
    assert result == in_flight  # unchanged


# ── poll_and_execute bootstrap (registers when no token) ─────────────────────


async def test_poll_and_execute_registers_when_no_token(monkeypatch: Any) -> None:
    import asyncio

    state: dict[str, Any] = {"poll_queue": []}
    captured: dict[str, Any] = {}

    settings = _settings(token="", install_token="INSTALL-1")
    stop = asyncio.Event()

    # Run exactly one tick: capture the headers the loop built post-registration,
    # then set the stop event so poll_and_execute returns gracefully.
    async def _one_tick(**kwargs: Any) -> set[Any]:
        captured["headers"] = kwargs["headers"]
        stop.set()
        return set()

    monkeypatch.setattr(worker_main, "run_once", _one_tick)
    monkeypatch.setattr(worker_main, "detect_capabilities", lambda: ["claude_code"])
    # Don't write a real .env into the repo during the test.
    monkeypatch.setattr(worker_main, "_persist_worker_token", lambda *a, **k: None)

    async with _client(state) as client:
        await worker_main.poll_and_execute(settings=settings, client=client, redis=None, stop=stop)

    # Registration happened (token minted) and the loop used it.
    assert ("POST", "/api/v1/workers/register") in state["calls"]
    assert captured["headers"]["X-Worker-Token"] == "WORKER-TOKEN"
    # The minted token was persisted back onto the settings object.
    assert settings.token == "WORKER-TOKEN"


# ── .env persistence ──────────────────────────────────────────────────────────


def test_update_env_file_upserts(tmp_path: Any) -> None:
    env = tmp_path / ".env"
    env.write_text("BSVIBE_WORKER_NAME=old\nUNRELATED=keep\n", encoding="utf-8")

    worker_main._update_env_file(
        str(env),
        {"BSVIBE_WORKER_TOKEN": "T", "BSVIBE_WORKER_NAME": "new"},
    )

    text = env.read_text(encoding="utf-8")
    assert "BSVIBE_WORKER_TOKEN=T" in text
    assert "BSVIBE_WORKER_NAME=new" in text
    assert "BSVIBE_WORKER_NAME=old" not in text
    assert "UNRELATED=keep" in text


def test_update_env_file_creates_when_absent(tmp_path: Any) -> None:
    env = tmp_path / ".env"
    worker_main._update_env_file(str(env), {"BSVIBE_WORKER_TOKEN": "X"})
    assert env.read_text(encoding="utf-8") == "BSVIBE_WORKER_TOKEN=X\n"


def test_persist_writes_key_that_settings_reads(tmp_path: Any, monkeypatch: Any) -> None:
    # Bug 2 regression: ``_persist_worker_token`` must write the SAME env key
    # that the settings field reads. A round-trip (persist -> reload from .env)
    # must populate ``settings.token`` so the worker does NOT re-register.
    env = tmp_path / ".env"
    monkeypatch.setattr(worker_main, "_ENV_PATH", str(env))

    settings = _settings(token="", name="rt-worker", server_url="http://rt")
    worker_main._persist_worker_token("MINTED-TOKEN", settings)

    # Reload a fresh WorkerSettings purely from the persisted .env (no overrides,
    # no leaking process env that could mask the bug).
    for key in ("BSVIBE_WORKER_TOKEN", "BSVIBE_WORKER_WORKER_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    reloaded = WorkerSettings(_env_file=str(env))

    assert reloaded.token == "MINTED-TOKEN"


# ── Full loop: error resilience + graceful stop ───────────────────────────────


async def test_poll_and_execute_loops_then_stops_on_event() -> None:
    # A real run_once over an empty poll queue runs a couple of ticks, then the
    # injected stop event ends the loop gracefully.
    import asyncio

    state: dict[str, Any] = {"poll_queue": []}
    stop = asyncio.Event()
    ticks = {"n": 0}

    orig_run_once = worker_main.run_once

    async def _counting_run_once(**kwargs: Any) -> set[Any]:
        ticks["n"] += 1
        result = await orig_run_once(**kwargs)
        if ticks["n"] >= 2:
            stop.set()
        return result

    async with _client(state) as client:
        # poll_interval_seconds=0 so _interruptible_sleep returns immediately.
        with _patched(worker_main, "run_once", _counting_run_once):
            await worker_main.poll_and_execute(
                settings=_settings(poll_interval_seconds=0),
                client=client,
                redis=None,
                stop=stop,
            )
    assert ticks["n"] >= 2


async def test_poll_and_execute_exits_on_401(monkeypatch: Any) -> None:
    import asyncio

    async def _raise_401(**kwargs: Any) -> set[Any]:
        request = httpx.Request("POST", "http://test/api/v1/workers/poll")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(worker_main, "run_once", _raise_401)
    state: dict[str, Any] = {}
    async with _client(state) as client:
        # Returns (does not hang) — a 401 means the token is bad, so the loop exits.
        await worker_main.poll_and_execute(
            settings=_settings(),
            client=client,
            redis=None,
            stop=asyncio.Event(),
        )


async def test_poll_and_execute_continues_past_transient_http_error() -> None:
    import asyncio

    stop = asyncio.Event()
    calls = {"n": 0}

    async def _flaky(**kwargs: Any) -> set[Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            request = httpx.Request("POST", "http://test/api/v1/workers/heartbeat")
            raise httpx.ConnectError("down", request=request)
        stop.set()  # second tick succeeds → stop
        return set()

    state: dict[str, Any] = {}
    async with _client(state) as client:
        with _patched(worker_main, "run_once", _flaky):
            await worker_main.poll_and_execute(
                settings=_settings(poll_interval_seconds=0),
                client=client,
                redis=None,
                stop=stop,
            )
    assert calls["n"] == 2  # survived the transient error and ran a second tick


async def test_interruptible_sleep_returns_immediately_for_zero() -> None:
    import asyncio

    await worker_main._interruptible_sleep(0, asyncio.Event())


async def test_interruptible_sleep_wakes_on_stop() -> None:
    import asyncio

    stop = asyncio.Event()
    stop.set()
    # Even with a long timeout, a pre-set stop returns at once.
    await worker_main._interruptible_sleep(3600, stop)


def test_connect_redis_none_when_no_url() -> None:
    assert worker_main._connect_redis(_settings(redis_url="")) is None


def _patched(obj: Any, name: str, value: Any) -> Any:
    """Tiny context manager to swap an attribute (avoids monkeypatch in helpers)."""
    import contextlib

    @contextlib.contextmanager
    def _ctx() -> Any:
        original = getattr(obj, name)
        setattr(obj, name, value)
        try:
            yield
        finally:
            setattr(obj, name, original)

    return _ctx()
