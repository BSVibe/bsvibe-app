"""Long-running ``opencode serve`` daemon — Lift E17.

The pre-E17 ``OpenCodeExecutor`` spawned ``opencode run --format json`` as a
fresh subprocess per task. Dogfood found this pays huge startup tax (workspace
scan, plugin load, tool registry init, agent runtime spin-up) on every call:
8 hours wall-clock on a trivial 1-line prompt. Against a long-running
``opencode serve`` + ``POST /session/{id}/message`` the same prompt finished
in 2.7 seconds; a 28k-token ingest prompt in 13 seconds; 200 calls @ 5-parallel
in 115 seconds with zero errors.

This module owns the daemon lifecycle:

* :func:`start_opencode_serve` — spawns ``opencode serve --port <p>
  --hostname <h>`` with ``start_new_session=True``, scrapes the listen URL
  from stdout, GET /openapi.json health-checks it, and returns a
  :class:`OpenCodeServerProcess` handle.
* :func:`stop_opencode_serve` — group-kills the daemon at worker shutdown via
  the shared :func:`_kill_process_group` helper (E15) so the daemon's children
  die alongside it.
* :func:`set_serve_url` / :func:`get_serve_url` / :func:`clear_serve_url` —
  a module-level singleton through which the worker startup hands the URL to
  :class:`OpenCodeExecutor`. No DI is needed at the executor seam: the worker
  is one process, one daemon, one URL.
* :func:`ensure_serve_running` — re-spawn helper used by the executor on
  connection-refused (daemon crashed mid-run). Single-attempt retry; the
  executor's caller decides whether to surface the second failure.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from backend.executors.worker.config import WorkerSettings
from backend.executors.worker.executors import (
    _kill_process_group,
    sanitized_subprocess_env,
)

logger = structlog.get_logger(__name__)


class OpenCodeServeStartupError(RuntimeError):
    """The serve daemon failed to start (no listening line, health 5xx, …)."""


@dataclass
class OpenCodeServerProcess:
    """A live ``opencode serve`` child process + its captured listen URL.

    Lift E23 — ``drain_tasks`` holds the background coroutines reading the
    daemon's stdout and stderr to /dev/null after the listening URL is
    captured. Without them, opencode's plugin chatter fills the 16 KiB OS
    pipe buffer (macOS), back-pressuring the asyncio event loop into a
    silent wedge — no heartbeats, no polls. ``stop_opencode_serve`` cancels
    them on shutdown.
    """

    process: asyncio.subprocess.Process
    url: str
    drain_tasks: tuple[asyncio.Task[None], ...] = ()


# ── Singleton: worker startup writes, executor reads ────────────────────────
#
# Stored as a single-slot dict so the get/set/clear helpers can mutate it
# without the ``global`` keyword (ruff PLW0603). The dict itself is the module
# attribute; only its single value moves.

_SINGLETON: dict[str, str] = {}


def set_serve_url(url: str) -> None:
    """Record the daemon's listen URL so the executor can find it."""
    _SINGLETON["url"] = url


def get_serve_url() -> str | None:
    """Return the daemon's listen URL, or ``None`` if startup never set it."""
    return _SINGLETON.get("url")


def clear_serve_url() -> None:
    """Drop the singleton (test isolation + worker shutdown)."""
    _SINGLETON.pop("url", None)


# ── Singleton: the live daemon handle (for SQLite-corruption recovery) ───────
#
# The URL singleton above is all the executor needs for the happy path. The
# corruption-recovery path additionally needs to STOP the running daemon (to
# release its lock on opencode.db before the db is moved aside), which requires
# the daemon handle — not just its URL. The worker startup publishes the handle
# here right after ``start_opencode_serve`` so the executor's recovery call can
# find it. Stored single-slot, mirroring the URL singleton, to dodge the
# ``global`` keyword (ruff PLW0603).

_DAEMON_SLOT: dict[str, OpenCodeServerProcess] = {}


def set_serve_daemon(daemon: OpenCodeServerProcess | None) -> None:
    """Publish (or clear, with ``None``) the live daemon handle."""
    if daemon is None:
        _DAEMON_SLOT.pop("daemon", None)
    else:
        _DAEMON_SLOT["daemon"] = daemon


def get_serve_daemon() -> OpenCodeServerProcess | None:
    """Return the live daemon handle, or ``None`` if startup never set it."""
    return _DAEMON_SLOT.get("daemon")


# ── Stdout-listening-line regex ─────────────────────────────────────────────

# ``opencode serve`` prints e.g. ``opencode server listening on http://127.0.0.1:54321``.
# We only care about the URL; ``opencode``/``opencode server`` style differences
# are tolerated.
_LISTENING_RE = re.compile(rb"listening on (http://[^\s]+)", re.IGNORECASE)


# ── Startup / shutdown ──────────────────────────────────────────────────────


async def start_opencode_serve(
    settings: WorkerSettings,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> OpenCodeServerProcess:
    """Spawn the ``opencode serve`` daemon, return its URL once healthy.

    Steps:

    1. Resolve the ``opencode`` CLI on PATH.
    2. ``asyncio.create_subprocess_exec`` with ``start_new_session=True`` so
       the daemon and every descendant (Bun runtime, helper processes) share
       a fresh process group — group-killable on shutdown (Lift E15).
    3. Scrape stdout until we see ``listening on <url>`` OR the startup
       timeout (``settings.opencode_serve_startup_timeout_s``) fires.
    4. ``GET /openapi.json`` against the captured URL; non-2xx → error.

    On any failure the process is group-killed before raising
    :class:`OpenCodeServeStartupError` so a half-broken daemon never leaks
    into the rest of the worker's life.

    ``http_transport`` lets tests stub the health check via an
    :class:`httpx.MockTransport`. Production passes ``None`` (real network).
    """
    cmd = shutil.which("opencode") or "opencode"
    # Lift E22 — do NOT pass ``--pure``. That flag skips loading external
    # plugins, which includes the ``opencode-go`` provider plugin that
    # registers the founder's subscribed models (qwen3.6-plus, kimi-k2.6,
    # …). Without it, every chat request to those models returns opencode's
    # ``UnknownError`` and the worker drops the task as exit 1 — discovered
    # in the E21 prod dogfood (2026-06-11). Standalone ``opencode serve``
    # (no ``--pure``) returns valid LLM responses for the same model id,
    # confirming the plugin path is the difference.
    argv = [
        cmd,
        "serve",
        "--hostname",
        settings.opencode_serve_host,
        "--port",
        str(settings.opencode_serve_port),
    ]
    env = sanitized_subprocess_env()
    logger.info(
        "opencode_serve_starting",
        host=settings.opencode_serve_host,
        port=settings.opencode_serve_port,
    )

    process = await asyncio.create_subprocess_exec(
        *argv,
        # Defense-in-depth (executor write isolation): pin the daemon to a
        # neutral temp cwd. Without this it inherits the worker's launchd
        # WorkingDirectory — the host's OWN source checkout — so a session
        # created without an explicit ``directory`` would treat the host repo as
        # its project root. Per-task code sessions still scope to their
        # workspace via the ``directory`` param on ``POST /session``.
        cwd=tempfile.gettempdir(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )

    try:
        url = await _await_listening_url(process, settings.opencode_serve_startup_timeout_s)
    except (TimeoutError, OpenCodeServeStartupError):
        logger.error("opencode_serve_startup_failed", reason="no_listening_line")
        _kill_process_group(process)
        raise OpenCodeServeStartupError(
            "opencode serve never printed its listening URL within "
            f"{settings.opencode_serve_startup_timeout_s}s"
        ) from None

    ok = await _health_check(url, http_transport=http_transport)
    if not ok:
        logger.error("opencode_serve_startup_failed", reason="health_check_failed", url=url)
        _kill_process_group(process)
        raise OpenCodeServeStartupError(f"opencode serve at {url} failed health check")

    # Lift E23 — drain the daemon's stdout + stderr in the background so the
    # OS pipe buffer (16 KiB on macOS) never saturates. Pre-E23 the worker
    # left both pipes unread after the listening line; once opencode's
    # plugin logging (which expanded after E22 dropped ``--pure``) crossed
    # the buffer threshold the asyncio event loop silently wedged — no
    # heartbeats, no polls, no further log lines, but the TCP connection
    # to the backend stayed ``ESTABLISHED`` so the failure was invisible
    # without a process sample.
    drain_tasks: tuple[asyncio.Task[None], ...] = (
        asyncio.create_task(_drain_stream(process.stdout), name="opencode-serve-stdout-drain"),
        asyncio.create_task(_drain_stream(process.stderr), name="opencode-serve-stderr-drain"),
    )

    logger.info("opencode_serve_ready", url=url, pid=process.pid)
    return OpenCodeServerProcess(process=process, url=url, drain_tasks=drain_tasks)


async def stop_opencode_serve(daemon: OpenCodeServerProcess) -> None:
    """Group-kill the daemon at worker shutdown.

    Best-effort — a daemon that already exited (``returncode`` set) is a no-op.
    Always swallows :class:`ProcessLookupError` (race between the group
    enumeration and the signal).

    Lift E23 — cancel the background drain tasks BEFORE the process is
    killed so they don't leak past the daemon's life. Cancellation is
    cooperative — each drain task suppresses ``asyncio.CancelledError`` so
    awaiting it after cancel returns cleanly.
    """
    # Cancel drain tasks unconditionally — even on a daemon that already
    # exited, the tasks may be parked on the EOF read.
    for task in daemon.drain_tasks:
        task.cancel()
    if daemon.drain_tasks:
        await asyncio.gather(*daemon.drain_tasks, return_exceptions=True)

    if daemon.process.returncode is not None:
        return
    try:
        _kill_process_group(daemon.process)
        try:
            await asyncio.wait_for(daemon.process.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("opencode_serve_shutdown_slow_wait", pid=daemon.process.pid)
        logger.info("opencode_serve_shutdown", pid=daemon.process.pid)
    except ProcessLookupError:  # pragma: no cover — race on shutdown
        pass


async def ensure_serve_running(settings: WorkerSettings) -> str:
    """Re-spawn the daemon if the singleton has no URL or the previous one died.

    Used by :class:`OpenCodeExecutor` on connection-refused. Returns the URL
    (writes it into the singleton as a side-effect). Single retry; the caller
    decides whether to escalate a second failure.
    """
    existing = get_serve_url()
    if existing:
        # The previous URL is still on file; the executor will retry against
        # it. If the daemon truly died, the retry will surface a fresh
        # ConnectError and the caller will fail terminally.
        logger.info("opencode_serve_existing_url_retained", url=existing)
        return existing
    daemon = await start_opencode_serve(settings)
    set_serve_url(daemon.url)
    return daemon.url


# ── SQLite-corruption recovery ──────────────────────────────────────────────
#
# opencode persists session state in a SQLite store. When the host's opencode
# binary is OLDER than whatever opencode last migrated that store with, the
# schema carries columns the binary doesn't expect (observed live on opencode
# 1.15.12: ``NOT NULL constraint failed: session_message.seq``). Every new
# session's first message insert then 500s, and the worker can never make
# progress until the db is reset by hand. ``restart_serve_after_corruption``
# automates the manual reset: stop the daemon (release its lock), move the
# store aside, start a fresh daemon (which recreates the store at the binary's
# own schema).

# The store + its SQLite WAL/SHM sidecars. All three move together so the fresh
# daemon never picks up a half-quarantined store.
_DB_FILES = ("opencode.db", "opencode.db-wal", "opencode.db-shm")


def opencode_data_dir(settings: WorkerSettings | None = None) -> Path:
    """Resolve opencode's data dir (where ``opencode.db`` lives).

    An explicit ``settings.opencode_data_dir`` wins; otherwise derive it the
    same way opencode itself does — ``$XDG_DATA_HOME/opencode`` when set, else
    ``~/.local/share/opencode``. Resolution mirrors the env the daemon was
    spawned with (``sanitized_subprocess_env`` preserves HOME + XDG).
    """
    if settings is not None and settings.opencode_data_dir:
        return Path(settings.opencode_data_dir)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def quarantine_opencode_db(data_dir: Path, *, suffix: str) -> list[Path]:
    """Move ``opencode.db`` (+ its WAL/SHM sidecars) aside, return the new paths.

    Best-effort: a sidecar that doesn't exist is skipped, and an entirely
    absent db is a no-op (returns ``[]``). Never raises on a missing file —
    recovery must not be blocked by the exact set of sidecars present.
    """
    moved: list[Path] = []
    for name in _DB_FILES:
        src = data_dir / name
        if not src.exists():
            continue
        dst = data_dir / f"{name}.bak-{suffix}"
        src.rename(dst)
        moved.append(dst)
    if moved:
        logger.warning(
            "opencode_db_quarantined",
            data_dir=str(data_dir),
            moved=[p.name for p in moved],
        )
    return moved


# How many of the most-recent log files to scan when resolving a ref. opencode
# rolls logs per serve boot; the ref we're chasing was logged by the daemon
# that just 500'd, so it's in one of the newest files.
_LOG_SCAN_LIMIT = 5


def lookup_server_error(ref: str, settings: WorkerSettings | None = None) -> str:
    """Resolve an opencode error ``ref`` to the line it logged, or ``""``.

    opencode's HTTP 500 body is generic (``Unexpected server error. Check
    server logs for details.``) and carries only a ``ref`` token; the REAL
    error (e.g. the SQLite seq violation) lives in opencode's own log under
    ``<data_dir>/log``. Scans the newest few log files for a line containing
    ``ref=<ref>`` and returns it. Best-effort — a missing/unreadable log dir
    yields ``""`` (the caller then can't confirm corruption and surfaces the
    error honestly instead of needlessly restarting serve). Never raises.
    """
    if not ref:
        return ""
    log_dir = opencode_data_dir(settings) / "log"
    try:
        logs = sorted(
            log_dir.glob("*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""
    needle = f"ref={ref}"
    for log in logs[:_LOG_SCAN_LIMIT]:
        try:
            content = log.read_text(errors="replace")
        except OSError:  # pragma: no cover — racing rotation
            continue
        # Scan newest-line-first: a ref can recur across retries; the latest
        # occurrence is the one that matches the 500 we're diagnosing.
        for line in reversed(content.splitlines()):
            if needle in line:
                return line
    return ""


def _corruption_suffix() -> str:
    """A unique-enough quarantine suffix (wall-clock; recovery is rare)."""
    import time  # noqa: PLC0415 — local to keep the hot import surface small

    return time.strftime("%Y%m%d-%H%M%S")


async def restart_serve_after_corruption(settings: WorkerSettings) -> str:
    """Stop the daemon, quarantine the SQLite store, start a fresh daemon.

    Order is load-bearing: STOP first so the daemon releases its lock on
    ``opencode.db``, THEN quarantine the store (on macOS moving a still-open
    file leaves the daemon on the now-unlinked inode — the corrupt schema
    survives), THEN start a fresh daemon which recreates the store at the
    binary's own schema. Re-publishes both singletons and returns the new URL.
    """
    old = get_serve_daemon()
    if old is not None:
        await stop_opencode_serve(old)
    set_serve_daemon(None)
    clear_serve_url()

    quarantine_opencode_db(opencode_data_dir(settings), suffix=_corruption_suffix())

    daemon = await start_opencode_serve(settings)
    set_serve_url(daemon.url)
    set_serve_daemon(daemon)
    logger.info("opencode_serve_restarted_after_corruption", url=daemon.url)
    return daemon.url


# ── Internals ───────────────────────────────────────────────────────────────


async def _await_listening_url(process: asyncio.subprocess.Process, timeout_s: float) -> str:
    """Read ``process.stdout`` lines until we see the listening URL or time out."""
    assert process.stdout is not None
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("opencode serve startup timed out")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        except TimeoutError:
            raise
        if not line:
            # EOF before we saw the listening line → the daemon died.
            raise OpenCodeServeStartupError("opencode serve exited before listening")
        match = _LISTENING_RE.search(line)
        if match:
            return match.group(1).decode("utf-8", errors="replace").rstrip("/")


async def _drain_stream(stream: asyncio.StreamReader | None) -> None:
    """Read + discard ``stream`` until EOF (Lift E23 pipe-drainer).

    Runs in a background task for the daemon's lifetime. Reads line by line
    — a chunked read would still drain the buffer, but ``readline`` matches
    the daemon's own output cadence (one structured log line per write) and
    keeps each await brief so cancellation is responsive. EOF (b\"\") ends
    the loop naturally; ``CancelledError`` from ``stop_opencode_serve`` is
    let to propagate so the awaiting caller can join the task.
    """
    if stream is None:  # pragma: no cover — only when subprocess started without PIPE
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — drain must not crash the worker
        logger.debug("opencode_serve_drain_error", exc_info=True)


async def _health_check(url: str, *, http_transport: httpx.AsyncBaseTransport | None) -> bool:
    """GET ``<url>/openapi.json``; return True on 2xx."""
    try:
        async with httpx.AsyncClient(base_url=url, transport=http_transport, timeout=5.0) as client:
            res = await client.get("/openapi.json")
            return 200 <= res.status_code < 300
    except httpx.HTTPError:
        return False


__all__ = [
    "OpenCodeServerProcess",
    "OpenCodeServeStartupError",
    "clear_serve_url",
    "ensure_serve_running",
    "get_serve_daemon",
    "get_serve_url",
    "lookup_server_error",
    "opencode_data_dir",
    "quarantine_opencode_db",
    "restart_serve_after_corruption",
    "set_serve_daemon",
    "set_serve_url",
    "start_opencode_serve",
    "stop_opencode_serve",
]
