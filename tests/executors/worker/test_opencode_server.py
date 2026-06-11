"""Tests for the long-running ``opencode serve`` daemon helper (Lift E17).

The worker spawns ``opencode serve --pure --port 0 --hostname 127.0.0.1`` as a
child process, captures the printed listen URL from its stdout, and health-
checks it via ``GET /openapi.json`` before letting the poll loop begin.

These tests stub :mod:`asyncio.create_subprocess_exec` so no real ``opencode``
binary is invoked. The daemon's stdout is a fake stream that emits the
``listening on http://...`` line (or never does, for the timeout path); the
health check is stubbed via :mod:`httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from backend.executors.worker import opencode_server
from backend.executors.worker.config import WorkerSettings

pytestmark = pytest.mark.asyncio


class _FakeStreamReader:
    """Minimal ``asyncio.StreamReader`` stand-in over a list of byte lines."""

    def __init__(self, lines: Sequence[bytes], *, hang_after: bool = False) -> None:
        self._lines = list(lines)
        self._hang_after = hang_after

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._hang_after:
            await asyncio.sleep(3600)
        return b""

    async def read(self, n: int = -1) -> bytes:
        return b""


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout_lines: Sequence[bytes],
        stderr_lines: Sequence[bytes] = (),
        returncode: int | None = None,
        hang_stdout_after: bool = True,
        pid: int = 4242,
    ) -> None:
        self.stdout = _FakeStreamReader(stdout_lines, hang_after=hang_stdout_after)
        self.stderr = _FakeStreamReader(stderr_lines, hang_after=hang_stdout_after)
        self._exit_rc = returncode
        self.returncode: int | None = None
        self.pid = pid
        self.killed = False
        self._wait_event = asyncio.Event()
        if returncode is not None:
            self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        self.returncode = self._exit_rc if self._exit_rc is not None else -9
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._wait_event.set()


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> list[dict[str, Any]]:
    """Patch ``asyncio.create_subprocess_exec`` to return ``proc``; return kwargs captured."""
    spawns: list[dict[str, Any]] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawns.append({"args": [str(a) for a in args], "kwargs": dict(kwargs)})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return spawns


def _ok_health_transport() -> httpx.MockTransport:
    """Mock transport that returns 200 on ``/openapi.json``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.0.0"})
        return httpx.Response(404)

    return httpx.MockTransport(_handler)


# ── start_opencode_serve ────────────────────────────────────────────────────


async def test_start_returns_url_after_stdout_line_and_health_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path — daemon prints listening URL, health check passes."""
    proc = _FakeProcess(
        stdout_lines=[
            b"opencode server listening on http://127.0.0.1:54321\n",
        ],
    )
    spawns = _patch_subprocess(monkeypatch, proc)

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    daemon = await opencode_server.start_opencode_serve(
        settings,
        http_transport=_ok_health_transport(),
    )

    assert daemon.url == "http://127.0.0.1:54321"
    assert daemon.process is proc  # type: ignore[comparison-overlap]
    # Spawned with start_new_session=True so the whole subtree can be killed.
    assert spawns[0]["kwargs"].get("start_new_session") is True
    argv = spawns[0]["args"]
    assert any("opencode" in a for a in argv)
    assert "serve" in argv


async def test_start_times_out_when_listening_line_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the daemon never prints the listening line, raise + kill the process."""
    proc = _FakeProcess(stdout_lines=[], hang_stdout_after=True)
    _patch_subprocess(monkeypatch, proc)
    # Stub group kill so the test doesn't try to signal a real pgrp.
    monkeypatch.setattr(opencode_server, "_kill_process_group", lambda p: p.kill())

    settings = WorkerSettings(opencode_serve_startup_timeout_s=0.2)
    with pytest.raises(opencode_server.OpenCodeServeStartupError):
        await opencode_server.start_opencode_serve(settings, http_transport=_ok_health_transport())
    assert proc.killed is True


async def test_start_raises_when_health_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listening line arrives but ``/openapi.json`` doesn't return 200 → error + kill."""
    proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:54322\n"])
    _patch_subprocess(monkeypatch, proc)
    monkeypatch.setattr(opencode_server, "_kill_process_group", lambda p: p.kill())

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    with pytest.raises(opencode_server.OpenCodeServeStartupError):
        await opencode_server.start_opencode_serve(
            settings, http_transport=httpx.MockTransport(_handler)
        )
    assert proc.killed is True


# ── stop_opencode_serve ─────────────────────────────────────────────────────


async def test_stop_kills_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:65000\n"])
    _patch_subprocess(monkeypatch, proc)
    killed_pgrps: list[int] = []

    def _record_and_kill(p: Any) -> None:
        killed_pgrps.append(p.pid)
        p.kill()

    monkeypatch.setattr(opencode_server, "_kill_process_group", _record_and_kill)

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    daemon = await opencode_server.start_opencode_serve(
        settings, http_transport=_ok_health_transport()
    )
    await opencode_server.stop_opencode_serve(daemon)

    assert killed_pgrps == [proc.pid]


# ── singleton wire (worker startup → executor read) ─────────────────────────


@pytest.mark.asyncio(loop_scope="function")
async def test_singleton_round_trip_set_and_get() -> None:
    """The module-level singleton is how the worker hands the URL to the executor."""
    opencode_server.clear_serve_url()
    assert opencode_server.get_serve_url() is None
    opencode_server.set_serve_url("http://127.0.0.1:8888")
    try:
        assert opencode_server.get_serve_url() == "http://127.0.0.1:8888"
    finally:
        opencode_server.clear_serve_url()


# ── argv shape ──────────────────────────────────────────────────────────────


async def test_serve_argv_loads_plugins_and_sets_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E22 — opencode serve MUST NOT be spawned with ``--pure``. The flag
    skips loading external plugins, including the ``opencode-go`` provider plugin
    that registers the founder's subscribed models (qwen3.6-plus, kimi-k2.6, etc.).
    Without those models registered, every chat request returns UnknownError.
    Pure-mode also bypasses the providers that ship as plugins (zen / opencode /
    opencode-go), so any model resolution falls through to nothing.
    """
    proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:60000\n"])
    spawns = _patch_subprocess(monkeypatch, proc)

    settings = WorkerSettings(
        opencode_serve_startup_timeout_s=5.0,
        opencode_serve_host="127.0.0.1",
        opencode_serve_port=0,
    )
    await opencode_server.start_opencode_serve(settings, http_transport=_ok_health_transport())

    argv = spawns[0]["args"]
    assert "--pure" not in argv, (
        "opencode serve must run WITH plugins so opencode-go provider is loaded"
    )
    assert "--port" in argv
    assert "0" in argv
    assert "--hostname" in argv
    assert "127.0.0.1" in argv
