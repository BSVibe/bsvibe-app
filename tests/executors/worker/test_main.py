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


class _FileWritingExecutor:
    """Writes files into the work dir it is handed (simulates a CLI producing output)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def supported_task_types(self) -> list[str]:
        return ["coding"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        work_dir = context["workspace_dir"]
        for rel, content in self._files.items():
            dest = os.path.join(work_dir, rel)
            os.makedirs(os.path.dirname(dest) or work_dir, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(content)
        yield ExecutionChunk(delta=f"ran:{prompt}")
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
            try:
                body = json.loads(request.content) if request.content else None
            except json.JSONDecodeError:
                body = None
            state.setdefault("heartbeats", []).append(body)
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
        "access_token": "",
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


async def test_register_sends_authorization_bearer() -> None:
    """Lift E4 — the worker emits ``Authorization: Bearer`` from the host cred."""
    state: dict[str, Any] = {}
    async with _client(state) as client:
        token = await worker_main.register(
            client,
            name="w1",
            bearer_token="ACCESS-TOKEN-XYZ",
            capabilities=["claude_code"],
        )
    assert token == "WORKER-TOKEN"
    assert state["register_headers"]["authorization"] == "Bearer ACCESS-TOKEN-XYZ"
    # Lift E5 — no install-token header is ever emitted.
    assert "x-install-token" not in state["register_headers"]


async def test_register_requires_bearer_token() -> None:
    """Lift E5 — without a bearer there is no legacy fallback; raises ValueError."""
    state: dict[str, Any] = {}
    async with _client(state) as client:
        with pytest.raises(ValueError, match="bearer_token"):
            await worker_main.register(client, name="w1", bearer_token="", capabilities=[])


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


async def test_handle_task_captures_produced_files_in_result(tmp_path: Any) -> None:
    """B1: files the executor writes into its work dir are collected and shipped
    in the ``/result`` POST body as base64-encoded ``files`` entries."""
    import base64

    executor = _FileWritingExecutor({"out.txt": b"hello world", "src/app.py": b"x = 1\n"})
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(prompt="build"),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            workspace_root=str(tmp_path),
        )
    body = state["results"][0]
    assert body["success"] is True
    files = {f["path"]: f for f in body["files"]}
    assert set(files) == {"out.txt", "src/app.py"}
    assert base64.b64decode(files["out.txt"]["content_b64"]) == b"hello world"
    assert base64.b64decode(files["src/app.py"]["content_b64"]) == b"x = 1\n"
    assert files["out.txt"]["truncated"] is False


async def test_handle_task_no_files_when_executor_writes_nothing(tmp_path: Any) -> None:
    """B1: an executor that produces no files reports an empty ``files`` list."""
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(prompt="noop"),
            executors={"claude_code": _StubExecutor()},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            workspace_root=str(tmp_path),
        )
    assert state["results"][0]["files"] == []


async def test_handle_task_skips_oversized_file_with_truncation_marker(tmp_path: Any) -> None:
    """B1: a file larger than the per-file cap is skipped (content empty) with a
    ``truncated: True`` marker — never shipped in full."""
    big = b"A" * (300 * 1024)  # > 256 KiB cap
    executor = _FileWritingExecutor({"big.bin": big, "small.txt": b"ok"})
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            _task(),
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            workspace_root=str(tmp_path),
        )
    files = {f["path"]: f for f in state["results"][0]["files"]}
    assert files["big.bin"]["truncated"] is True
    assert files["big.bin"]["content_b64"] == ""
    assert files["small.txt"]["truncated"] is False


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


# ── Lift E16 — heartbeat carries in_flight count ─────────────────────────────


async def test_run_once_heartbeat_reports_in_flight_zero_when_idle(monkeypatch: Any) -> None:
    """Lift E16 — an idle worker heartbeats ``in_flight=0`` (after reaping done tasks)."""
    state: dict[str, Any] = {"poll_queue": []}
    monkeypatch.setattr(worker_main, "select_executor", lambda _t: _StubExecutor())
    async with _client(state) as client:
        await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=set(),
        )
    assert state["heartbeats"] == [{"in_flight": 0}]


async def test_run_once_heartbeat_reports_in_flight_at_cap_when_saturated() -> None:
    """Lift E16 — a saturated worker reports its in-flight count so the backend can exclude it.

    The poll loop still SKIPS polling at-cap (the worker has no slot), but
    it MUST heartbeat with the real count so the backend's
    :func:`find_available_worker` sees the saturation signal and stops
    dispatching onto this stream. Without this, the worker silently
    swallows tasks the backend keeps XADDing — backend timer expires
    before the worker reads them.
    """
    state: dict[str, Any] = {"poll_queue": [_task()]}

    class _NeverDone:
        def done(self) -> bool:
            return False

    in_flight = {_NeverDone(), _NeverDone(), _NeverDone()}
    async with _client(state) as client:
        await worker_main.run_once(
            client=client,
            settings=_settings(max_parallel_tasks=3),
            executors={"claude_code": _StubExecutor()},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=in_flight,  # type: ignore[arg-type]
        )
    assert state["heartbeats"] == [{"in_flight": 3}]


# ── poll_and_execute bootstrap (registers when no token) ─────────────────────


async def test_poll_and_execute_registers_when_no_token(monkeypatch: Any) -> None:
    import asyncio

    state: dict[str, Any] = {"poll_queue": []}
    captured: dict[str, Any] = {}

    # Lift E5 — the host OAuth bearer is the only register credential.
    settings = _settings(token="", access_token="HOST-BEARER")
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
    # The register call carried the host OAuth bearer (Lift E4) — never an
    # X-Install-Token header (Lift E5 removed that path entirely).
    assert state["register_headers"]["authorization"] == "Bearer HOST-BEARER"
    assert "x-install-token" not in state["register_headers"]
    assert captured["headers"]["X-Worker-Token"] == "WORKER-TOKEN"
    # The minted token was persisted back onto the settings object.
    assert settings.token == "WORKER-TOKEN"


async def test_poll_and_execute_raises_when_no_token_and_no_bearer(monkeypatch: Any) -> None:
    """Lift E5 — without a worker token AND without a host bearer, register cannot run."""
    import asyncio

    monkeypatch.setattr(worker_main, "detect_capabilities", lambda: ["claude_code"])
    monkeypatch.setattr(worker_main, "_persist_worker_token", lambda *a, **k: None)
    # No CredentialsNotFound: simulate "no host credentials on disk".
    from backend.executors.worker import credentials as worker_credentials

    def _no_creds() -> Any:
        raise worker_credentials.CredentialsNotFound("no host credentials")

    monkeypatch.setattr(worker_main, "load_host_credentials", _no_creds)
    # Also block the saved-token-file fallback path so the test exercises the
    # "truly no credentials anywhere" branch — the autouse conftest fixture
    # already redirects BSVIBE_HOME, but be explicit here.
    monkeypatch.setattr(worker_main, "load_worker_token", lambda: None, raising=False)

    settings = _settings(token="", access_token="")
    state: dict[str, Any] = {"poll_queue": []}
    async with _client(state) as client:
        with pytest.raises(RuntimeError, match="bsvibe login"):
            await worker_main.poll_and_execute(
                settings=settings, client=client, redis=None, stop=asyncio.Event()
            )


async def test_poll_and_execute_loads_saved_worker_token_when_settings_token_empty(
    monkeypatch: Any,
) -> None:
    """Lift E8 Bug 4: when ``settings.token`` is empty BUT a previous
    ``bsvibe-worker register`` saved a token to ``~/.bsvibe/worker.token``,
    ``poll_and_execute`` MUST pick the saved token up and skip auto-registration.

    Pre-fix behaviour: the worker re-registered on every ``bsvibe-worker run``
    because ``settings.token`` only sources ``BSVIBE_WORKER_TOKEN`` (env), never
    the file ``register`` writes. That created duplicate workers + ModelAccount
    rows in the founder's workspace on each run.
    """
    import asyncio

    from backend.executors.worker import credentials as worker_credentials

    # Pre-seed the saved-token file at the autouse-redirected default path so
    # ``load_worker_token()`` (no path=) returns it.
    worker_credentials.save_worker_token("SAVED-WORKER-TOKEN")

    state: dict[str, Any] = {"poll_queue": []}
    captured: dict[str, Any] = {}
    stop = asyncio.Event()

    async def _one_tick(**kwargs: Any) -> set[Any]:
        captured["headers"] = kwargs["headers"]
        stop.set()
        return set()

    monkeypatch.setattr(worker_main, "run_once", _one_tick)
    monkeypatch.setattr(worker_main, "detect_capabilities", lambda: ["claude_code"])

    # If the worker INCORRECTLY re-registers we want a loud failure, not a
    # silent register-then-overwrite — make register() blow up.
    async def _explode(*_a: Any, **_kw: Any) -> str:
        raise AssertionError(
            "register() was called — Bug 4 regressed: the saved worker token "
            "was not picked up before the auto-register branch."
        )

    monkeypatch.setattr(worker_main, "register", _explode)

    settings = _settings(token="", access_token="")
    async with _client(state) as client:
        await worker_main.poll_and_execute(settings=settings, client=client, redis=None, stop=stop)

    # The saved token flowed into the request headers WITHOUT a register call.
    assert captured["headers"]["X-Worker-Token"] == "SAVED-WORKER-TOKEN"
    assert ("POST", "/api/v1/workers/register") not in state.get("calls", [])
    # And the settings object now carries the loaded token (so a subsequent
    # tick inside the same process sees it directly without another file read).
    assert settings.token == "SAVED-WORKER-TOKEN"


# ── Token persistence (Lift E12 — CWD .env writeback removed) ─────────────────


def test_persist_worker_token_writes_only_to_home_token_file(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Lift E12 — ``_persist_worker_token`` writes the token under
    ``~/.bsvibe/`` only. The legacy CWD ``.env`` upsert (E4/E8) has been
    removed: the founder's qazasa123 dogfood proved that splitting state
    between ``~/.bsvibe/worker.token`` and a CWD-relative ``.env`` silently
    loses the register-time config when ``run`` is invoked from a different
    CWD.

    This test asserts (1) the home-dir token file IS written and (2) NO
    ``.env`` file appears in the CWD as a side effect.
    """
    from pathlib import Path

    from backend.executors.worker.credentials import default_worker_token_path

    monkeypatch.chdir(tmp_path)

    default_path = default_worker_token_path()
    real_home_path = Path.home() / ".bsvibe" / "worker.token"
    assert default_path != real_home_path, (
        "BSVIBE_HOME redirect failed — default token path still resolves to "
        "the real home; the conftest autouse fixture is not active."
    )

    real_existed_before = real_home_path.exists()
    real_mtime_before = real_home_path.stat().st_mtime_ns if real_existed_before else None

    settings = _settings(token="", name="hot-worker", server_url="http://hot")
    worker_main._persist_worker_token("HOTFIX-TOKEN", settings)

    assert default_path.exists()
    assert default_path.read_text(encoding="utf-8").strip() == "HOTFIX-TOKEN"

    # No ``.env`` left in the CWD — the legacy writeback is gone.
    assert not (tmp_path / ".env").exists()

    # The real ``~/.bsvibe/worker.token`` was NOT touched.
    if real_existed_before:
        assert real_home_path.stat().st_mtime_ns == real_mtime_before
        assert real_home_path.read_text(encoding="utf-8").strip() != "HOTFIX-TOKEN"
    else:
        assert not real_home_path.exists()


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


# ── Lift E14 — cancel signal handling + subprocess lifecycle ────────────────


class _BlockingExecutor:
    """Streams forever (until ``aclose``) so the test can cancel mid-stream."""

    def __init__(self) -> None:
        self.entered = False
        self.cancelled = False
        self.closed = False

    def supported_task_types(self) -> list[str]:
        return ["claude_code"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        import asyncio

        self.entered = True
        try:
            yield ExecutionChunk(delta="starting")
            while True:
                await asyncio.sleep(60)  # blocks forever
                yield ExecutionChunk(delta="tick")
        except (GeneratorExit, BaseException):
            # ``GeneratorExit`` from ``aclose``; ``CancelledError`` from task.cancel().
            self.cancelled = True
            raise
        finally:
            self.closed = True


async def test_handle_task_cancellation_skips_result_post(tmp_path: Any) -> None:
    """Lift E14 — when the backend cancels an in-flight task (the asyncio
    Task running :func:`handle_task` is .cancel()-ed), the worker MUST:

    1. Let CancelledError propagate through the streaming executor so its
       subprocess cleanup runs (kills the child process).
    2. NOT POST a result to ``/api/v1/workers/result`` — the backend has
       already moved on; a late ``failed`` result POST would clobber the
       row the backend may have already terminal-flipped.
    3. Log ``task_cancelled_by_backend`` so the cancel path is visible
       in production logs.
    """
    import asyncio

    state: dict[str, Any] = {}
    executor = _BlockingExecutor()

    async def _run() -> None:
        async with _client(state) as client:
            await worker_main.handle_task(
                _task(executor_type="claude_code"),
                executors={"claude_code": executor},
                client=client,
                headers={"X-Worker-Token": "WORKER-TOKEN"},
                redis=None,
                workspace_root=str(tmp_path),
            )

    task = asyncio.create_task(_run())
    # Let the executor enter its loop before we cancel.
    for _ in range(50):
        if executor.entered:
            break
        await asyncio.sleep(0.01)
    assert executor.entered, "executor never entered its loop — test setup bug"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Subprocess-equivalent cleanup ran (executor's finally fired).
    assert executor.closed is True
    # Result was NOT posted to the backend — the cancel path bypasses it.
    assert state.get("results") is None or state.get("results") == []


async def test_handle_task_registers_in_flight_for_cancel_lookup(tmp_path: Any) -> None:
    """Lift E14 — :func:`handle_task` registers its asyncio.Task in
    :data:`backend.executors.worker.main._RUNNING_TASKS` under the
    task_id so the poll-loop cancel-action handler can look it up and
    cancel it. The dict entry is removed on exit (success / failure /
    cancellation) so it does not leak.
    """
    import asyncio

    state: dict[str, Any] = {}
    task_payload = _task(prompt="hi")
    task_id = task_payload["task_id"]
    executor = _StubExecutor()
    async with _client(state) as client:
        # Wrap in a task so handle_task's own asyncio.current_task() is the wrapper.
        coro_task = asyncio.create_task(
            worker_main.handle_task(
                task_payload,
                executors={"claude_code": executor},
                client=client,
                headers={"X-Worker-Token": "WORKER-TOKEN"},
                redis=None,
                workspace_root=str(tmp_path),
            )
        )
        await coro_task
    # After completion the registry must NOT still hold the task.
    assert task_id not in worker_main._RUNNING_TASKS


async def test_run_once_cancel_action_cancels_in_flight_task(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Lift E14 — when a poll returns a ``{action: cancel, task_id: X}``
    message, the loop looks X up in the in-flight registry and calls
    .cancel() on the running asyncio.Task instead of spawning a new
    handler.
    """
    import asyncio

    # Step 1: spawn a long-running task and wait until it's in-flight.
    state: dict[str, Any] = {"poll_queue": [_task(prompt="long")]}
    executor = _BlockingExecutor()
    monkeypatch.setattr(worker_main, "select_executor", lambda _t: executor)

    async with _client(state) as client:
        in_flight = await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={"claude_code": executor},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=set(),
        )
        assert len(in_flight) == 1

        for _ in range(50):
            if executor.entered:
                break
            await asyncio.sleep(0.01)
        assert executor.entered, "first poll didn't actually start the task"

        # Step 2: a second poll returns a cancel for the same task_id.
        # Reconstruct the task_id from in_flight registry — the only place we have it.
        in_flight_task_ids = list(worker_main._RUNNING_TASKS.keys())
        assert len(in_flight_task_ids) == 1
        target_task_id = in_flight_task_ids[0]

        state["poll_queue"] = [
            {
                "task_id": target_task_id,
                "action": "cancel",
                "dispatched_at": "2026-05-24T00:00:00+00:00",
            }
        ]
        await worker_main.run_once(
            client=client,
            settings=_settings(),
            executors={"claude_code": executor},
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            in_flight=in_flight,
        )

        # Drain the original task — must finish (cancelled).
        for t in in_flight:
            with pytest.raises(asyncio.CancelledError):
                await t

    # Executor's finally fired → subprocess equivalent cleanup ran.
    assert executor.closed is True
    # Registry cleared.
    assert target_task_id not in worker_main._RUNNING_TASKS
    # Worker did NOT POST a late ``failed`` result for the cancelled task.
    assert state.get("results") is None or state["results"] == []


# ── Lift E14 — shutdown propagates to running subprocesses ──────────────────


async def test_poll_and_execute_cancels_in_flight_on_stop(monkeypatch: Any, tmp_path: Any) -> None:
    """Lift E14 — when the stop event is set (signal / shutdown), any
    asyncio.Tasks still in flight get .cancel()-ed so their streaming
    executors run their subprocess-cleanup finally blocks. Without this,
    an SIGTERM would orphan the spawned ``claude --print`` / ``opencode``
    subprocesses (the dogfood found 7 of these alive 22 h after their
    parent worker daemon died)."""
    import asyncio

    state: dict[str, Any] = {"poll_queue": []}
    executor = _BlockingExecutor()
    monkeypatch.setattr(worker_main, "select_executor", lambda _t: executor)
    monkeypatch.setattr(worker_main, "detect_capabilities", lambda: ["claude_code"])
    monkeypatch.setattr(worker_main, "_persist_worker_token", lambda *a, **k: None)

    stop = asyncio.Event()

    # Replace run_once with one that spawns a single long-running task on
    # the first tick, then sets stop on the second tick.
    real_run_once = worker_main.run_once
    ticks = {"n": 0}

    async def _run_once(**kwargs: Any) -> Any:
        ticks["n"] += 1
        if ticks["n"] == 1:
            state["poll_queue"] = [_task(executor_type="claude_code", prompt="forever")]
        else:
            state["poll_queue"] = []
            stop.set()
        result = await real_run_once(**kwargs)
        if ticks["n"] == 1:
            for _ in range(50):
                if executor.entered:
                    break
                await asyncio.sleep(0.01)
        return result

    monkeypatch.setattr(worker_main, "run_once", _run_once)

    settings = _settings(poll_interval_seconds=0)
    async with _client(state) as client:
        await worker_main.poll_and_execute(settings=settings, client=client, redis=None, stop=stop)

    # The blocking executor's finally MUST have run — subprocess cleanup
    # is what frees the orphaned ``claude --print`` / ``opencode``
    # processes the dogfood found alive 22h after worker death.
    assert executor.closed is True


def test_ensure_process_group_skipped_on_windows(monkeypatch: Any) -> None:
    """Lift E14 — ``_ensure_process_group`` is a no-op on Windows."""
    import sys

    called = {"n": 0}

    def _spy() -> None:
        called["n"] += 1

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("os.setpgrp", _spy, raising=False)
    worker_main._ensure_process_group()
    assert called["n"] == 0


def test_ensure_process_group_calls_setpgrp_on_posix(monkeypatch: Any) -> None:
    """Lift E14 — on POSIX, ``_ensure_process_group`` calls ``os.setpgrp()``
    so the worker daemon becomes a process group leader. This lets a
    future OS-level supervisor (systemd KillMode=control-group, launchd)
    terminate the whole group atomically, preventing the orphaned
    ``opencode run`` subprocesses the dogfood found alive 22 h after
    their parent worker daemon died.
    """
    import os
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    called = {"n": 0}

    def _spy() -> None:
        called["n"] += 1

    monkeypatch.setattr(os, "setpgrp", _spy, raising=False)
    worker_main._ensure_process_group()
    assert called["n"] == 1


async def test_cancel_all_running_tasks_cancels_pending_handlers(tmp_path: Any) -> None:
    """Lift E14 — signal handler helper iterates :data:`_RUNNING_TASKS` and
    calls ``.cancel()`` on each non-done entry. Done tasks are left alone.
    """
    import asyncio

    state: dict[str, Any] = {}
    executor = _BlockingExecutor()
    async with _client(state) as client:
        running_task = asyncio.create_task(
            worker_main.handle_task(
                _task(),
                executors={"claude_code": executor},
                client=client,
                headers={"X-Worker-Token": "WORKER-TOKEN"},
                redis=None,
                workspace_root=str(tmp_path),
            )
        )
        for _ in range(50):
            if executor.entered:
                break
            await asyncio.sleep(0.01)
        assert executor.entered

        # Helper fires .cancel() on the in-flight task.
        worker_main._cancel_all_running_tasks()

        with pytest.raises(asyncio.CancelledError):
            await running_task

    assert executor.closed is True


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
