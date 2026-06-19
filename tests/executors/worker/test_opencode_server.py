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
from pathlib import Path
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
        # E23 — track bytes consumed by drain tasks so tests can assert
        # the daemon's stdout/stderr is actively read, not left to fill the
        # OS pipe buffer (16 KiB on macOS → asyncio event-loop wedge once
        # opencode's plugin logging crosses that threshold).
        self.consumed_bytes = 0

    async def readline(self) -> bytes:
        if self._lines:
            line = self._lines.pop(0)
            self.consumed_bytes += len(line)
            return line
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


# ── SQLite-corruption auto-recovery (this lift) ─────────────────────────────
#
# opencode persists session state in a SQLite store. When the binary on the
# host is OLDER than whatever opencode last migrated that store with, the
# schema carries columns the running binary doesn't expect (observed live:
# ``NOT NULL constraint failed: session_message.seq`` on opencode 1.15.12 vs a
# newer-migrated db). Every new session's first message insert then 500s and
# the worker can never make progress until a human resets the db. The recovery
# primitive here lets the worker self-heal: stop the daemon (releasing its
# lock on the db), move the corrupt db aside, start a fresh daemon (which
# recreates the store at the binary's own schema).


@pytest.mark.asyncio(loop_scope="function")
async def test_daemon_handle_singleton_round_trip() -> None:
    """The recovery path needs the live daemon handle (to stop it), so the
    worker startup must publish it into a module singleton alongside the URL."""
    opencode_server.set_serve_daemon(None)
    assert opencode_server.get_serve_daemon() is None
    sentinel = object()
    opencode_server.set_serve_daemon(sentinel)  # type: ignore[arg-type]
    try:
        assert opencode_server.get_serve_daemon() is sentinel
    finally:
        opencode_server.set_serve_daemon(None)
        assert opencode_server.get_serve_daemon() is None


async def test_opencode_data_dir_honours_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
    assert opencode_server.opencode_data_dir() == Path("/custom/xdg/opencode")


async def test_opencode_data_dir_defaults_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/worker")))
    assert opencode_server.opencode_data_dir() == Path("/home/worker/.local/share/opencode")


async def test_opencode_data_dir_setting_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``opencode_data_dir`` setting wins over XDG/HOME derivation."""
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
    settings = WorkerSettings(opencode_data_dir="/explicit/dir")
    assert opencode_server.opencode_data_dir(settings) == Path("/explicit/dir")


async def test_quarantine_moves_db_and_sidecars(tmp_path: Path) -> None:
    """The db plus its WAL/SHM sidecars are all moved aside so the fresh daemon
    starts from a clean store. The originals must be gone; the quarantined
    copies must retain the bytes."""
    data_dir = tmp_path / "opencode"
    data_dir.mkdir()
    (data_dir / "opencode.db").write_bytes(b"corrupt-db")
    (data_dir / "opencode.db-wal").write_bytes(b"wal")
    (data_dir / "opencode.db-shm").write_bytes(b"shm")

    moved = opencode_server.quarantine_opencode_db(data_dir, suffix="20260620-000000")

    assert not (data_dir / "opencode.db").exists()
    assert not (data_dir / "opencode.db-wal").exists()
    assert not (data_dir / "opencode.db-shm").exists()
    # The moved-aside db retains the original bytes.
    bak = data_dir / "opencode.db.bak-20260620-000000"
    assert bak.read_bytes() == b"corrupt-db"
    assert bak in moved


async def test_quarantine_tolerates_missing_sidecars(tmp_path: Path) -> None:
    """A db with no WAL/SHM (or even no db at all) must not raise — recovery is
    best-effort and never blocks the restart."""
    data_dir = tmp_path / "opencode"
    data_dir.mkdir()
    (data_dir / "opencode.db").write_bytes(b"db-only")

    moved = opencode_server.quarantine_opencode_db(data_dir, suffix="s")

    assert not (data_dir / "opencode.db").exists()
    assert (data_dir / "opencode.db.bak-s").read_bytes() == b"db-only"
    assert all(p.exists() for p in moved)

    # No db present at all → no-op, no raise.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert opencode_server.quarantine_opencode_db(empty, suffix="s2") == []


async def test_restart_after_corruption_stops_quarantines_then_starts_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``restart_serve_after_corruption`` must, in order: stop the existing
    daemon (so its lock on the db is released), quarantine the db, start a
    fresh daemon, and republish both the URL and daemon singletons."""
    order: list[str] = []

    old_proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:1111\n"])
    old_daemon = opencode_server.OpenCodeServerProcess(
        process=old_proc, url="http://127.0.0.1:1111"
    )
    opencode_server.set_serve_daemon(old_daemon)
    opencode_server.set_serve_url("http://127.0.0.1:1111")

    new_proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:2222\n"])
    new_daemon = opencode_server.OpenCodeServerProcess(
        process=new_proc, url="http://127.0.0.1:2222"
    )

    async def _fake_stop(daemon: Any) -> None:
        assert daemon is old_daemon
        order.append("stop")

    def _fake_quarantine(data_dir: Path, *, suffix: str) -> list[Path]:
        order.append("quarantine")
        return [data_dir / "opencode.db.bak"]

    async def _fake_start(settings: Any, **kwargs: Any) -> Any:
        order.append("start")
        return new_daemon

    monkeypatch.setattr(opencode_server, "stop_opencode_serve", _fake_stop)
    monkeypatch.setattr(opencode_server, "quarantine_opencode_db", _fake_quarantine)
    monkeypatch.setattr(opencode_server, "start_opencode_serve", _fake_start)
    monkeypatch.setattr(opencode_server, "opencode_data_dir", lambda settings=None: tmp_path)

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    try:
        url = await opencode_server.restart_serve_after_corruption(settings)

        assert url == "http://127.0.0.1:2222"
        # Stop BEFORE quarantine (db lock must be released first), quarantine
        # BEFORE start (fresh store), start last.
        assert order == ["stop", "quarantine", "start"]
        assert opencode_server.get_serve_url() == "http://127.0.0.1:2222"
        assert opencode_server.get_serve_daemon() is new_daemon
    finally:
        opencode_server.set_serve_daemon(None)
        opencode_server.clear_serve_url()


# ── ref → server-log correlation (corruption is hidden behind a generic 500) ─
#
# opencode's HTTP 500 body is generic — ``{"data":{"message":"Unexpected
# server error. Check server logs for details.","ref":"err_69ca699e"}}``. The
# REAL error (e.g. the SQLite seq violation) is only in opencode's own log,
# keyed by that ``ref``. ``lookup_server_error`` resolves a ref to its logged
# line so the executor can tell a store-corruption 500 apart from any other.

_LOG_LINE = (
    "ERROR 2026-06-19T16:04:51 +73ms service=server ref={ref} "
    "error=NOT NULL constraint failed: session_message.seq "
    "cause=SQLiteError: NOT NULL constraint failed: session_message.seq"
)


async def test_lookup_server_error_finds_ref_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "opencode"
    log_dir = data_dir / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-06-19T160338.log").write_text(
        "INFO  service=server msg=boot\n" + _LOG_LINE.format(ref="err_69ca699e") + "\n"
    )
    monkeypatch.setattr(opencode_server, "opencode_data_dir", lambda settings=None: data_dir)

    line = opencode_server.lookup_server_error("err_69ca699e")
    assert "session_message.seq" in line
    assert "ref=err_69ca699e" in line


async def test_lookup_server_error_missing_ref_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "opencode"
    log_dir = data_dir / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "a.log").write_text(_LOG_LINE.format(ref="err_aaaa") + "\n")
    monkeypatch.setattr(opencode_server, "opencode_data_dir", lambda settings=None: data_dir)

    assert opencode_server.lookup_server_error("err_does_not_exist") == ""


async def test_lookup_server_error_scans_latest_log_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ref lives in the NEWEST log; older logs may reuse opencode's rolling
    filenames. Resolution must find it regardless of how many logs exist."""
    data_dir = tmp_path / "opencode"
    log_dir = data_dir / "log"
    log_dir.mkdir(parents=True)
    old = log_dir / "2026-06-19T150000.log"
    new = log_dir / "2026-06-19T160338.log"
    old.write_text("INFO old log, no refs here\n")
    new.write_text(_LOG_LINE.format(ref="err_13a393a7") + "\n")
    # Make the mtimes unambiguous (new is newer).
    import os as _os

    _os.utime(old, (1_000_000, 1_000_000))
    _os.utime(new, (2_000_000, 2_000_000))
    monkeypatch.setattr(opencode_server, "opencode_data_dir", lambda settings=None: data_dir)

    line = opencode_server.lookup_server_error("err_13a393a7")
    assert "session_message.seq" in line


async def test_lookup_server_error_no_log_dir_is_best_effort_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No log dir (or unreadable) → empty string, never raises. The executor
    then can't confirm corruption and surfaces the error honestly rather than
    needlessly restarting serve."""
    monkeypatch.setattr(
        opencode_server, "opencode_data_dir", lambda settings=None: tmp_path / "nonexistent"
    )
    assert opencode_server.lookup_server_error("err_whatever") == ""


# ── argv shape ──────────────────────────────────────────────────────────────


async def test_start_drains_stdout_and_stderr_after_url_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E23 — once the listening URL is captured, the daemon's stdout AND
    stderr MUST be drained in the background. Otherwise opencode's plugin
    chatter (now louder post-E22 with plugins enabled) fills the OS pipe
    buffer (16 KiB on macOS), back-pressuring the asyncio event loop into a
    silent wedge — no heartbeats, no polls, no further log lines.

    The fix: ``start_opencode_serve`` returns a daemon whose stdout/stderr
    each have a background reader task consuming them so the buffer never
    saturates. Discovered via the E22 prod dogfood (2026-06-12).
    """
    extra_chatter = [
        b"timestamp=... level=INFO message=stream providerID=opencode-go\n",
        b"timestamp=... level=INFO message=llm runtime selected\n",
        b"timestamp=... level=DEBUG message=tool registered name=read\n",
    ]
    proc = _FakeProcess(
        stdout_lines=[
            b"opencode server listening on http://127.0.0.1:54400\n",
            *extra_chatter,
        ],
        stderr_lines=list(extra_chatter),
    )
    _patch_subprocess(monkeypatch, proc)

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    daemon = await opencode_server.start_opencode_serve(
        settings, http_transport=_ok_health_transport()
    )

    # Give the drain tasks a slice to consume queued lines.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert proc.stdout.consumed_bytes > 0, (
        "post-URL stdout chatter must be drained in the background — otherwise "
        "the pipe buffer fills and the asyncio loop wedges"
    )
    assert proc.stderr.consumed_bytes > 0, (
        "stderr must also be drained — opencode's plugin logs write to stderr "
        "more than stdout once --pure is dropped (E22)"
    )

    # Drain tasks are owned by the daemon handle so shutdown can cancel them.
    drain_tasks = getattr(daemon, "drain_tasks", None)
    assert drain_tasks, "daemon handle must expose its drain tasks for shutdown"
    assert all(isinstance(t, asyncio.Task) for t in drain_tasks)

    # Cancel + wait so the test doesn't leak a forever-sleeping task.
    for t in drain_tasks:
        t.cancel()
    for t in drain_tasks:
        try:
            await t
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass


async def test_stop_cancels_drain_tasks_before_killing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E23 — ``stop_opencode_serve`` cancels the background drain tasks so
    they don't leak past the daemon's life. They MUST be cancelled (or already
    done) before the process is group-killed."""
    proc = _FakeProcess(
        stdout_lines=[b"opencode server listening on http://127.0.0.1:54500\n"],
    )
    _patch_subprocess(monkeypatch, proc)
    monkeypatch.setattr(opencode_server, "_kill_process_group", lambda p: p.kill())

    settings = WorkerSettings(opencode_serve_startup_timeout_s=5.0)
    daemon = await opencode_server.start_opencode_serve(
        settings, http_transport=_ok_health_transport()
    )
    drain_tasks = list(daemon.drain_tasks)  # type: ignore[attr-defined]
    assert drain_tasks, "daemon must own drain tasks"

    await opencode_server.stop_opencode_serve(daemon)

    for t in drain_tasks:
        assert t.done(), "drain tasks must be cancelled/done after stop"


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
