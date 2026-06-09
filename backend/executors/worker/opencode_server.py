"""Long-running ``opencode serve`` daemon — Lift E17.

The pre-E17 ``OpenCodeExecutor`` spawned ``opencode run --format json`` as a
fresh subprocess per task. Dogfood found this pays huge startup tax (workspace
scan, plugin load, tool registry init, agent runtime spin-up) on every call:
8 hours wall-clock on a trivial 1-line prompt. Against a long-running
``opencode serve`` + ``POST /session/{id}/message`` the same prompt finished
in 2.7 seconds; a 28k-token ingest prompt in 13 seconds; 200 calls @ 5-parallel
in 115 seconds with zero errors.

This module owns the daemon lifecycle:

* :func:`start_opencode_serve` — spawns ``opencode serve --pure --port <p>
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
import re
import shutil
from dataclasses import dataclass

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
    """A live ``opencode serve`` child process + its captured listen URL."""

    process: asyncio.subprocess.Process
    url: str


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
    argv = [
        cmd,
        "serve",
        "--pure",
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

    logger.info("opencode_serve_ready", url=url, pid=process.pid)
    return OpenCodeServerProcess(process=process, url=url)


async def stop_opencode_serve(daemon: OpenCodeServerProcess) -> None:
    """Group-kill the daemon at worker shutdown.

    Best-effort — a daemon that already exited (``returncode`` set) is a no-op.
    Always swallows :class:`ProcessLookupError` (race between the group
    enumeration and the signal).
    """
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
    "get_serve_url",
    "set_serve_url",
    "start_opencode_serve",
    "stop_opencode_serve",
]
