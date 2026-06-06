"""BSVibe executor worker — poll loop, registration, task handling.

Headless client process. On first run (no worker token) it registers with the
backend using the host OAuth bearer (Lift E4 — ``bsvibe login`` writes
``~/.config/bsvibe/credentials.json``), persisting the returned worker token
to ``.env``. Then it loops::

    heartbeat -> poll(count=free slots) -> for each task:
        select executor by ``executor_type`` -> run it -> collect output
        -> [optionally publish stream chunks to task:{id}:stream]
        -> POST /api/v1/workers/result -> [publish task:{id}:done]

Bounded concurrency (``max_parallel_tasks``) and graceful SIGINT/SIGTERM
shutdown. The functions are factored so the loop is testable without a real
backend (inject an ``httpx.AsyncClient`` over a MockTransport) or a real Redis
(inject ``None`` or a fake) — see ``tests/executors/worker/test_main.py``.

Usage::

    python -m backend.executors.worker

Lift M3 (v8 §20.4 Pattern C audit, 2026-06-02) — **SRP-clean, skipped.**
Pattern C = worker file bundling config + business logic + poll-loop
boilerplate. The worker config dataclass (:class:`WorkerSettings`) is
already extracted to ``backend.executors.worker.config``; the executor
selection + capability detection are extracted to
``backend.executors.worker.executors``. What remains here is the
process entry point: registration, the single-task body
(:func:`handle_task`), the one-tick body (:func:`run_once`), the main
loop (:func:`poll_and_execute`), tiny ``.env`` upsert helpers, and the
signal-wired :func:`main` entry point. These are bundled because this
file *is* the headless worker process — splitting registration /
artifact-capture / loop / wiring into 4 modules creates 4 modules with
1-2 functions each and no shared seam beyond the process itself. The
narrow ``_RedisPublisher`` Protocol is a port defined where used. The
public surface (:func:`handle_task`, :func:`poll_and_execute`,
:func:`register`, :func:`run_once`) keeps every existing test injection
point. No split needed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import structlog

from backend.executors.worker.config import WorkerSettings, get_worker_settings
from backend.executors.worker.credentials import (
    CredentialsNotFound,
    load_host_credentials,
    save_worker_token,
)
from backend.executors.worker.executors import (
    ExecutorProtocol,
    detect_capabilities,
    select_executor,
)

logger = structlog.get_logger(__name__)

_ENV_PATH = ".env"
_HTTP_TIMEOUT_S = 30.0

# Artifact-capture caps (executor-pool B1). The per-task work dir starts EMPTY
# for executor tasks, so every regular file in it is the CLI's output — but cap
# the count + per-file size so a runaway build (node_modules, large binaries)
# can't blow up the result POST. A file over the byte cap is reported as a
# truncation marker (empty content + ``truncated: True``), never shipped in full.
_MAX_CAPTURED_FILES = 100
_MAX_FILE_BYTES = 256 * 1024


#: Directory names + suffixes that are build/cache junk, never real artifacts.
_JUNK_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"})
_JUNK_SUFFIXES = (".pyc", ".pyo")


def _is_build_junk(rel: Path) -> bool:
    """True for build/cache artifacts (any segment a junk dir, or a junk suffix)."""
    if rel.suffix in _JUNK_SUFFIXES:
        return True
    return any(part in _JUNK_DIRS for part in rel.parts)


def _collect_workspace_files(work_dir: str) -> list[dict[str, Any]]:
    """Walk ``work_dir`` and return the files the CLI produced (B1).

    The dir starts empty for executor tasks, so every regular file is treated as
    output. Each entry is ``{path, content_b64, truncated}`` where ``path`` is
    relative to ``work_dir`` (POSIX-style). Symlinks are skipped (never follow
    out of the work dir); files over :data:`_MAX_FILE_BYTES` are reported as a
    truncation marker with empty content. At most :data:`_MAX_CAPTURED_FILES`
    files are returned (deterministic sort so the cap is stable).

    Build/cache junk is skipped (``__pycache__`` / ``.pytest_cache`` dirs, ``.pyc``
    files): an agent that RUNS its tests leaves these behind, they aren't real
    deliverables, and being binary they would poison a downstream text consumer
    (the design→impl handoff prompt → a Postgres text column).
    """
    root = Path(work_dir)
    if not root.is_dir():
        return []
    candidates = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and not _is_build_junk(p.relative_to(root))
    )
    collected: list[dict[str, Any]] = []
    for path in candidates[:_MAX_CAPTURED_FILES]:
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            logger.warning("artifact_stat_failed", path=rel)
            continue
        if size > _MAX_FILE_BYTES:
            logger.info("artifact_skipped_oversized", path=rel, size=size)
            collected.append({"path": rel, "content_b64": "", "truncated": True})
            continue
        try:
            content = path.read_bytes()
        except OSError:
            logger.warning("artifact_read_failed", path=rel)
            continue
        collected.append(
            {
                "path": rel,
                "content_b64": base64.b64encode(content).decode("ascii"),
                "truncated": False,
            }
        )
    if len(candidates) > _MAX_CAPTURED_FILES:
        logger.info(
            "artifact_capture_truncated",
            work_dir=work_dir,
            total=len(candidates),
            kept=_MAX_CAPTURED_FILES,
        )
    return collected


async def _finalize_task(
    stream: Any, local_workspace: str, *, task_id: Any
) -> list[dict[str, Any]]:
    """Close the executor stream, capture produced files, then remove the work dir.

    Returns the captured ``files`` (B1) — collected BEFORE the rmtree, else the
    CLI's output is lost. Both close and capture are best-effort: a failure here
    must never crash the loop or drop the result POST.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
    files: list[dict[str, Any]] = []
    try:
        files = await asyncio.to_thread(_collect_workspace_files, local_workspace)
    except Exception:  # noqa: BLE001 — capture is best-effort, never fails a task
        logger.warning("artifact_capture_failed", task_id=task_id, exc_info=True)
    shutil.rmtree(local_workspace, ignore_errors=True)
    return files


class _RedisPublisher(Protocol):
    """The narrow Redis surface the worker needs (publish only)."""

    async def publish(self, channel: str, message: str) -> Any: ...


# ── Registration ──────────────────────────────────────────────────────────────


async def register(
    client: httpx.AsyncClient,
    *,
    name: str,
    capabilities: list[str],
    bearer_token: str,
    labels: list[str] | None = None,
) -> str:
    """Register this worker → return the worker token (Lift E4).

    ``bearer_token`` is the host OAuth credential (Supabase session JWT or
    MCP access token), sent as ``Authorization: Bearer <token>``. The
    backend derives the workspace from the verified claims.

    The returned worker token is what every later request uses (heartbeat /
    poll / result).
    """
    if not bearer_token:
        raise ValueError("register() needs a bearer_token; run `bsvibe login` to produce one.")
    payload: dict[str, Any] = {
        "name": name,
        "capabilities": capabilities or ["claude_code"],
        "labels": labels or [],
    }
    headers = {"Authorization": f"Bearer {bearer_token}"}
    res = await client.post("/api/v1/workers/register", json=payload, headers=headers)
    res.raise_for_status()
    data = res.json()
    token: str = data["token"]
    logger.info("worker_registered", worker_id=data.get("id"), name=name)
    return token


# ── Single-task handling ───────────────────────────────────────────────────────


async def handle_task(
    task: dict[str, Any],
    *,
    executors: dict[str, ExecutorProtocol],
    client: httpx.AsyncClient,
    headers: dict[str, str],
    redis: _RedisPublisher | None,
    workspace_root: str | None = None,
) -> None:
    """Execute one polled task, stream chunks (if redis), and POST the result.

    The worker runs each task in a FRESH, isolated local directory it creates
    here (under ``workspace_root`` if set, else the OS temp dir) and removes in
    the ``finally``. The ``workspace_dir`` in the dispatched payload is the
    BACKEND container's run path — a foreign absolute path that does not exist on
    this (remote) machine — so it is intentionally ignored: the executor's cwd is
    always the worker-local dir.

    B1: before removing that local dir, the worker walks it and collects the
    files the CLI produced (the dir started empty, so every regular file is
    output), base64-encoding each so binary and text carry safely. They ship in
    the ``/result`` POST body as ``files`` alongside the existing output/success/
    error fields; the backend persists them under the run workspace and records
    real ``artifact_refs`` (see :func:`backend.executors.dispatch.record_result`).
    """
    task_id = task["task_id"]
    prompt = task.get("prompt") or ""
    executor_type = task.get("executor_type") or "claude_code"
    stream_chan = task.get("stream_channel") or f"task:{task_id}:stream"
    done_chan = task.get("done_channel") or f"task:{task_id}:done"

    logger.info("task_received", task_id=task_id, executor=executor_type)

    executor = executors.get(executor_type)
    if executor is None:
        executor = select_executor(executor_type)
        executors[executor_type] = executor

    local_workspace = tempfile.mkdtemp(prefix="bsvibe-task-", dir=workspace_root or None)
    context: dict[str, Any] = {
        "task_id": task_id,
        # ALWAYS the worker-local dir — never the backend's foreign run path.
        "workspace_dir": local_workspace,
        "system": task.get("system") or "",
        # ``model`` is not part of the current dispatch payload; forwarded when
        # present for forward-compatibility (CLI default otherwise).
        "model": task.get("model") or None,
    }

    parts: list[str] = []
    error: str | None = None
    success = True
    files: list[dict[str, Any]] = []
    stream = executor.execute(prompt, context)
    try:
        async for chunk in stream:
            if chunk.delta:
                parts.append(chunk.delta)
            if chunk.error:
                error = chunk.error
                success = False
            if redis is not None:
                await _publish(
                    redis,
                    stream_chan,
                    {"delta": chunk.delta, "done": chunk.done, "error": chunk.error},
                )
            if chunk.done:
                break
    except Exception as exc:  # noqa: BLE001 — defensive; report rather than crash the loop
        error = str(exc)
        success = False
        if redis is not None:
            await _publish(redis, stream_chan, {"delta": "", "done": True, "error": error})
    finally:
        # Close the stream, CAPTURE produced files (B1), then remove the work
        # dir — order matters: capture must precede the rmtree.
        files = await _finalize_task(stream, local_workspace, task_id=task_id)

    await client.post(
        "/api/v1/workers/result",
        headers=headers,
        json={
            "task_id": task_id,
            "success": success,
            "output": "".join(parts),
            "error_message": error,
            "files": files,
        },
    )
    if redis is not None:
        await _publish(
            redis,
            done_chan,
            {"task_id": task_id, "success": success, "error_message": error},
        )
    logger.info("task_completed", task_id=task_id, success=success)


async def _publish(redis: _RedisPublisher, channel: str, payload: dict[str, Any]) -> None:
    try:
        await redis.publish(channel, json.dumps(payload))
    except Exception:  # noqa: BLE001 — streaming is best-effort, never fails a task
        logger.warning("pubsub_publish_failed", channel=channel, exc_info=True)


# ── One poll-loop tick ──────────────────────────────────────────────────────────


async def run_once(
    *,
    client: httpx.AsyncClient,
    settings: WorkerSettings,
    executors: dict[str, ExecutorProtocol],
    headers: dict[str, str],
    redis: _RedisPublisher | None,
    in_flight: set[asyncio.Task[None]],
) -> set[asyncio.Task[None]]:
    """One tick: reap done tasks, heartbeat, poll (if free slots), spawn tasks.

    Returns the updated ``in_flight`` set. Heartbeat always fires; poll is
    skipped when already at ``max_parallel_tasks``. Each polled task is run in a
    background task so the loop never blocks on a slow executor.
    """
    in_flight = {t for t in in_flight if not t.done()}

    await client.post("/api/v1/workers/heartbeat", headers=headers)

    max_parallel = settings.max_parallel_tasks
    if len(in_flight) >= max_parallel:
        return in_flight

    slots = max_parallel - len(in_flight)
    res = await client.post(
        "/api/v1/workers/poll",
        headers=headers,
        params={"count": min(slots, settings.poll_batch_max)},
    )
    res.raise_for_status()
    tasks: list[dict[str, Any]] = res.json()
    if not tasks:
        return in_flight

    async def _run(task: dict[str, Any]) -> None:
        try:
            await handle_task(
                task,
                executors=executors,
                client=client,
                headers=headers,
                redis=redis,
                workspace_root=settings.workspace_root or None,
            )
        except Exception:  # noqa: BLE001 — one task's failure must not kill the loop
            logger.exception("task_execution_error", task_id=task.get("task_id"))

    for task in tasks:
        in_flight.add(asyncio.create_task(_run(task)))
    return in_flight


# ── Main loop ─────────────────────────────────────────────────────────────────


async def poll_and_execute(
    *,
    settings: WorkerSettings,
    client: httpx.AsyncClient,
    redis: _RedisPublisher | None,
    stop: asyncio.Event | None = None,
) -> None:
    """Register-if-needed then loop ``run_once`` until ``stop`` (or forever).

    The HTTP ``client`` + ``redis`` are injected so the loop is fully testable.
    A ``stop`` event lets a signal handler (or a test) end the loop gracefully;
    in-flight tasks are awaited before returning.
    """
    token = settings.token
    if not token:
        logger.info("no_worker_token", hint="registering with backend")
        bearer = _resolve_host_bearer(settings)
        if not bearer:
            raise RuntimeError("no host OAuth credential; run `bsvibe login` on this host first.")
        token = await register(
            client,
            name=settings.name,
            bearer_token=bearer,
            capabilities=detect_capabilities(),
        )
        settings.token = token
        _persist_worker_token(token, settings)

    executors: dict[str, ExecutorProtocol] = {}
    for cap in detect_capabilities():
        try:
            executors[cap] = select_executor(cap)
        except ValueError:
            # Detected but no executor in this lift (codex / opencode) — skip.
            logger.info("capability_detected_no_executor", capability=cap)

    headers = {"X-Worker-Token": token}
    in_flight: set[asyncio.Task[None]] = set()
    stop = stop or asyncio.Event()

    logger.info(
        "worker_starting",
        name=settings.name,
        server=settings.server_url,
        executors=list(executors.keys()),
        streaming=redis is not None,
    )

    try:
        while not stop.is_set():
            try:
                in_flight = await run_once(
                    client=client,
                    settings=settings,
                    executors=executors,
                    headers=headers,
                    redis=redis,
                    in_flight=in_flight,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    logger.error("auth_failed", hint="invalid worker token; re-register")
                    return
                logger.error("http_error", status=exc.response.status_code)
            except httpx.HTTPError:
                logger.warning("server_unreachable", url=settings.server_url)
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.exception("worker_loop_error")
            await _interruptible_sleep(settings.poll_interval_seconds, stop)
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)


async def _interruptible_sleep(seconds: float, stop: asyncio.Event) -> None:
    """Sleep up to ``seconds``, waking early if ``stop`` is set."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


# ── .env persistence ────────────────────────────────────────────────────────────


def _resolve_host_bearer(settings: WorkerSettings) -> str | None:
    """Return the OAuth bearer for register, or ``None`` when absent.

    Looks up ``settings.access_token`` first (``BSVIBE_WORKER_ACCESS_TOKEN``)
    then the host credentials file (``~/.config/bsvibe/credentials.json``)
    that ``bsvibe login`` writes. ``None`` means the caller must run
    ``bsvibe login`` first — there is no legacy fallback.
    """
    if settings.access_token:
        return settings.access_token
    try:
        creds = load_host_credentials()
    except CredentialsNotFound:
        return None
    return creds.access_token


def _persist_worker_token(token: str, settings: WorkerSettings) -> None:
    """Write the freshly minted worker token to ``.env`` + the dedicated file.

    Lift E4 — the GitHub-Actions-runner UX also persists the token at
    ``~/.bsvibe/worker.token`` (mode 0600) so subsequent invocations of
    ``bsvibe-worker run`` can pick it up without depending on a project
    ``.env``. The ``.env`` write is kept for legacy hosts that wire the
    config that way.
    """
    updates = {
        "BSVIBE_WORKER_TOKEN": token,
        "BSVIBE_WORKER_NAME": settings.name,
        "BSVIBE_WORKER_SERVER_URL": settings.server_url,
    }
    _update_env_file(_ENV_PATH, updates)
    try:
        save_worker_token(token)
    except OSError:  # pragma: no cover — file-system failure is non-fatal
        logger.warning("worker_token_file_save_failed", exc_info=True)


def _update_env_file(path: str, updates: dict[str, str]) -> None:
    """Idempotently upsert key=value lines into a ``.env`` file."""
    env_path = Path(path)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    env_path.write_text("".join(out), encoding="utf-8")


# ── Real-world wiring (entry point) ──────────────────────────────────────────────


def _connect_redis(settings: WorkerSettings) -> _RedisPublisher | None:
    if not settings.redis_url:
        return None
    try:
        from redis.asyncio import Redis as _Redis  # noqa: PLC0415 — optional streaming dep

        # ``redis.asyncio.Redis`` satisfies ``_RedisPublisher`` at runtime (it has
        # an awaitable ``publish``); its declared overload-broad signature doesn't
        # structurally match the narrow Protocol, so cast at the boundary.
        return cast("_RedisPublisher", _Redis.from_url(settings.redis_url, decode_responses=False))
    except Exception:  # noqa: BLE001 — streaming is optional; degrade gracefully
        logger.warning("redis_connect_failed", url=settings.redis_url, exc_info=True)
        return None


async def _amain() -> None:
    settings = get_worker_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):  # pragma: no cover — non-main thread / Windows
            pass

    redis = _connect_redis(settings)
    if redis is None and not settings.redis_url:
        logger.info(
            "no_redis",
            hint="set BSVIBE_WORKER_REDIS_URL to stream chunks back to the backend",
        )
    async with httpx.AsyncClient(base_url=settings.server_url, timeout=_HTTP_TIMEOUT_S) as client:
        try:
            await poll_and_execute(settings=settings, client=client, redis=redis, stop=stop)
        finally:
            if redis is not None:
                aclose = getattr(redis, "aclose", None)
                if aclose is not None:
                    await aclose()


def main() -> None:
    """Synchronous entry point for ``python -m backend.executors.worker``."""
    asyncio.run(_amain())


__all__ = [
    "handle_task",
    "poll_and_execute",
    "register",
    "run_once",
]
