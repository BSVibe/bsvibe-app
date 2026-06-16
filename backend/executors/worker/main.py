"""BSVibe executor worker — poll loop, registration, task handling.

Headless client process. On first run (no worker token) it registers with the
backend using the host OAuth bearer (Lift E4 — ``bsvibe login`` writes
``~/.config/bsvibe/credentials.json``), persisting the returned worker token
to ``~/.bsvibe/worker.token`` (Lift E12 retired the CWD ``.env`` writeback).
Then it loops::

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
loop (:func:`poll_and_execute`), the worker-token persistence helper, and the
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
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import structlog

from backend.executors.worker import opencode_server
from backend.executors.worker.config import WorkerSettings, get_worker_settings
from backend.executors.worker.credentials import (
    CredentialsNotFound,
    load_host_credentials,
    load_worker_config,
    load_worker_token,
    save_worker_token,
)
from backend.executors.worker.executors import (
    ExecutorProtocol,
    detect_capabilities,
    select_executor,
)

logger = structlog.get_logger(__name__)

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


#: Lift E14 — in-flight task registry keyed by ``task_id`` string.
#:
#: When the backend's :class:`~backend.dispatch.adapter.ExecutorAdapter`
#: times out waiting on a worker, it XADDs an ``action=cancel`` message
#: onto the worker's stream. The worker's next poll surfaces that
#: message, looks the ``task_id`` up in this dict, and calls
#: ``.cancel()`` on the wrapper :class:`asyncio.Task`. The wrapper's
#: cleanup runs the streaming executor's ``finally`` (which kills the
#: CLI subprocess). This decouples the cancel transport (a stream
#: message) from the cleanup mechanism (asyncio cancellation +
#: per-executor subprocess teardown) — no per-executor cancel hook is
#: needed.
#:
#: Module-level so a signal handler in :func:`_amain` can iterate it
#: at shutdown and cancel every still-running task before the process
#: exits. The dict is single-event-loop-scoped (one asyncio loop per
#: worker process) so plain Python dict access is safe.
_RUNNING_TASKS: dict[str, asyncio.Task[None]] = {}


def _is_build_junk(rel: Path) -> bool:
    """True for build/cache artifacts (any segment a junk dir, or a junk suffix)."""
    if rel.suffix in _JUNK_SUFFIXES:
        return True
    return any(part in _JUNK_DIRS for part in rel.parts)


def _collect_workspace_files(work_dir: str) -> list[dict[str, Any]]:
    """Walk ``work_dir`` and return the files the CLI produced (B1, legacy).

    Used by chat-shaped callers (no ``repo_url`` → no git checkout) where
    the work dir starts EMPTY and every regular file IS the CLI's output.
    Sorted with a hard ``_MAX_CAPTURED_FILES`` cap so the result POST stays
    bounded.

    Lift E33 — when the dispatcher cloned a repo into the work dir
    (1500+ files for bsvibe-app), this walker hits its cap on file 100
    sorted alphabetically and CANNOT tell apart "files the agent edited"
    from "files that were in the original checkout". The E32 dogfood (run
    a180f51e) proved it: all six executor tasks captured the same first
    100 files (``.devcontainer/Dockerfile`` …), zero agent edits visible.
    For the repo-cloned path use :func:`_collect_changed_files` instead.

    Build/cache junk is skipped (``__pycache__`` / ``.pytest_cache`` dirs,
    ``.pyc`` files): an agent that RUNS its tests leaves these behind,
    they aren't real deliverables.
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


async def _collect_changed_files(work_dir: str) -> list[dict[str, Any]]:
    """Lift E33 — capture ONLY the files the agent changed (git diff against
    the post-clone baseline) instead of walking the whole work dir.

    Coding agents (opencode / codex / claude_code) work the way a human
    developer does: read files on demand with their own Read tool, edit
    with their own Edit tool, run bash. They DON'T spit "everything in
    workspace" as the deliverable; the deliverable is the diff. Walking
    the whole work_dir and capping at 100 files captures whichever files
    sort first alphabetically (``.devcontainer/Dockerfile`` …) — the
    actual edits land off-screen.

    Approach: ``git status --porcelain`` against the freshly-cloned repo
    enumerates only modified/added/deleted/renamed paths. Read each, ship
    the same ``{path, content_b64, truncated}`` shape as B1 so the
    record_result + artifact_store path stays unchanged. Deleted files
    are reported with empty content + ``deleted: True`` so the backend
    can record the removal explicitly.

    Returns ``[]`` when:
    - the work dir isn't a git repo (chat-shaped fallback should have
      gone through :func:`_collect_workspace_files` instead),
    - ``git status`` fails for any reason (degrades gracefully — soft-
      fail rather than killing the task).
    """
    root = Path(work_dir)
    if not (root / ".git").exists():
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            # ``-u``/``--untracked-files=all`` expands untracked DIRECTORIES
            # to each contained file. Without this, an agent that creates
            # a new subdir would yield one porcelain line ending in ``/``
            # (the directory marker) — we'd treat it as a file path,
            # stat() the dir, and ship nothing.
            "--untracked-files=all",
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
    except Exception:  # noqa: BLE001 — soft-fail keeps the run path alive
        logger.warning("artifact_git_status_failed", work_dir=work_dir, exc_info=True)
        return []
    if proc.returncode != 0:
        logger.warning(
            "artifact_git_status_nonzero",
            work_dir=work_dir,
            returncode=proc.returncode,
        )
        return []

    collected: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        # Porcelain shape: ``XY <path>`` where ``XY`` is the two-char
        # index/worktree status. Take the rightmost path so ``R old -> new``
        # captures the new name. We don't need to distinguish staged vs
        # unstaged since a fresh clone has none staged.
        if len(line) < 4:
            continue
        status_code = line[:2]
        path_segment = line[3:].strip()
        rel = path_segment.rsplit(" -> ", 1)[-1]
        if not rel or _is_build_junk(Path(rel)):
            continue
        abs_path = root / rel
        deleted = status_code[0] == "D" or status_code[1] == "D"
        if deleted:
            collected.append({"path": rel, "content_b64": "", "truncated": False, "deleted": True})
            continue
        try:
            size = abs_path.stat().st_size
        except OSError:
            logger.warning("artifact_stat_failed", path=rel)
            continue
        if size > _MAX_FILE_BYTES:
            logger.info("artifact_skipped_oversized", path=rel, size=size)
            collected.append({"path": rel, "content_b64": "", "truncated": True})
            continue
        try:
            content = abs_path.read_bytes()
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
    logger.info(
        "artifact_changed_files_captured",
        work_dir=work_dir,
        captured=len(collected),
    )
    return collected


async def _finalize_task(
    stream: Any, local_workspace: str, *, task_id: Any
) -> list[dict[str, Any]]:
    """Close the executor stream, capture produced files, then remove the work dir.

    Returns the captured ``files`` (B1) — collected BEFORE the rmtree, else the
    CLI's output is lost. Both close and capture are best-effort: a failure here
    must never crash the loop or drop the result POST.

    Lift E33 — when the work dir has a ``.git`` (the E32 clone path), capture
    only the files git knows changed since the clone. The plain walker keeps
    serving chat-shaped callers whose work dir is the legacy empty tempdir.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:  # noqa: BLE001, S110 — cleanup best-effort
            pass
    files: list[dict[str, Any]] = []
    try:
        if (Path(local_workspace) / ".git").exists():
            files = await _collect_changed_files(local_workspace)
        else:
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


async def _clone_repo_into_workspace(repo_url: str, workspace_dir: str) -> None:
    """Lift E32 — shallow-clone ``repo_url`` into the per-task workspace.

    ``git clone --depth 1 <repo_url> <workspace_dir>`` would fail because
    ``workspace_dir`` already exists (``tempfile.mkdtemp`` created it).
    ``git init`` + ``git fetch`` + ``git checkout FETCH_HEAD`` is the
    idiomatic recovery: it clones into an existing-but-empty dir without
    the ``destination path already exists`` error and stays shallow.

    Soft-fail: any subprocess error degrades to the empty-tempdir
    behaviour so a transient network/git issue doesn't take down the
    whole task. The agent will see no files and likely write nothing,
    but the run still terminates rather than hanging.
    """
    cmds = [
        ["git", "init", "-q"],
        ["git", "fetch", "--depth=1", "--quiet", repo_url, "HEAD"],
        ["git", "checkout", "--quiet", "FETCH_HEAD"],
    ]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
        except Exception:  # noqa: BLE001 — best-effort clone
            logger.warning(
                "repo_clone_subprocess_failed", repo_url=repo_url, cmd=cmd[0], exc_info=True
            )
            return
        if proc.returncode != 0:
            logger.warning(
                "repo_clone_step_failed",
                repo_url=repo_url,
                cmd=cmd[0],
                returncode=proc.returncode,
                stderr=stderr.decode("utf-8", errors="replace")[:500],
            )
            return
    logger.info("repo_cloned", repo_url=repo_url, workspace_dir=workspace_dir)


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
    # Lift E32 — when the dispatcher told the worker about a repo URL,
    # shallow-clone it into ``local_workspace`` BEFORE handing the
    # workspace to the executor. Without this the coding agent
    # (opencode / codex / claude_code) gets an empty tempdir and the
    # E31 dogfood symptom returns: success=True per call but ``git
    # status --short`` empty, ``artifact_refs`` NULL — the agent had
    # nothing to read or edit. Soft-fails: a clone error degrades to
    # the empty-tempdir behaviour rather than killing the task, so a
    # transient network blip doesn't take down the whole agent_loop.
    repo_url = task.get("repo_url") or None
    if repo_url:
        await _clone_repo_into_workspace(repo_url, local_workspace)
    context: dict[str, Any] = {
        "task_id": task_id,
        # ALWAYS the worker-local dir — never the backend's foreign run path.
        "workspace_dir": local_workspace,
        "system": task.get("system") or "",
        # ``model`` is not part of the current dispatch payload; forwarded when
        # present for forward-compatibility (CLI default otherwise).
        "model": task.get("model") or None,
    }

    # Lift E14 — register the asyncio Task this handler runs in so the
    # poll-loop cancel-action handler can look us up by ``task_id`` and
    # ``.cancel()`` us. Inside the wrapper :func:`_run` in
    # :func:`run_once` the current task IS the wrapper; outside of that
    # (e.g. tests that call ``handle_task`` directly without wrapping)
    # ``current_task()`` is still some Task and the registration is
    # harmless. Skip when we somehow are not inside a Task at all.
    current = asyncio.current_task()
    if current is not None:
        _RUNNING_TASKS[task_id] = current
    try:
        outcome = await _stream_and_collect(
            executor=executor,
            prompt=prompt,
            context=context,
            stream_chan=stream_chan,
            redis=redis,
            task_id=task_id,
            local_workspace=local_workspace,
        )
        await client.post(
            "/api/v1/workers/result",
            headers=headers,
            json={
                "task_id": task_id,
                "success": outcome.success,
                "output": "".join(outcome.parts),
                "error_message": outcome.error,
                "files": outcome.files,
            },
        )
        if redis is not None:
            await _publish(
                redis,
                done_chan,
                {"task_id": task_id, "success": outcome.success, "error_message": outcome.error},
            )
        logger.info("task_completed", task_id=task_id, success=outcome.success)
    except asyncio.CancelledError:
        # Lift E14 — the poll-loop saw an ``action=cancel`` for this
        # task_id (backend timed out / shutdown signal fired) and
        # ``.cancel()``-ed our wrapper task. The executor's ``finally``
        # in :func:`_stream_and_collect` has already killed the CLI
        # subprocess + removed the work dir. Skip the result POST — the
        # backend has already moved on, a late ``failed`` POST would
        # clobber a row it may have already terminal-flipped.
        logger.info("task_cancelled_by_backend", task_id=task_id, executor=executor_type)
        raise
    finally:
        _RUNNING_TASKS.pop(task_id, None)


@dataclass
class _StreamOutcome:
    """Aggregated result of one streaming-executor drain (Lift E14)."""

    success: bool
    parts: list[str]
    error: str | None
    files: list[dict[str, Any]]


async def _stream_and_collect(
    *,
    executor: ExecutorProtocol,
    prompt: str,
    context: dict[str, Any],
    stream_chan: str,
    redis: _RedisPublisher | None,
    task_id: str,
    local_workspace: str,
) -> _StreamOutcome:
    """Drain the executor's chunk stream, then finalize + capture files.

    Split out of :func:`handle_task` for branch-count hygiene (PLR0912):
    the chunk loop, its cancel/error handling, and the post-loop
    stream-close + workspace-capture step all live here so the caller is
    a flat happy-path + result POST.

    Re-raises :class:`asyncio.CancelledError` so the caller can
    short-circuit the result POST + emit a ``task_cancelled_by_backend``
    log entry. The ``finally`` runs even on cancellation, so the CLI
    subprocess + work dir cleanup always fires.
    """
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
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — defensive; report rather than crash the loop
        error = str(exc)
        success = False
        if redis is not None:
            await _publish(redis, stream_chan, {"delta": "", "done": True, "error": error})
    finally:
        files = await _finalize_task(stream, local_workspace, task_id=task_id)
    return _StreamOutcome(success=success, parts=parts, error=error, files=files)


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

    # Lift E16 — heartbeat carries the worker's current in-flight count
    # so the backend's :func:`find_available_worker` can capacity-exclude
    # saturated workers. Pre-E16 the heartbeat had no body and the backend
    # had no visibility into worker load — it kept dispatching onto a
    # stream the worker's poll loop had paused (the poll-skip-at-cap path
    # below), and the per-task timer expired before the worker ever read
    # the task. Reporting the count here is the producer side of the
    # capacity gate.
    await client.post(
        "/api/v1/workers/heartbeat",
        headers=headers,
        json={"in_flight": len(in_flight)},
    )

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
        except asyncio.CancelledError:
            # Lift E14 — cancel propagated from the poll-loop cancel-action
            # handler (or shutdown). The handler has already cleaned up
            # the subprocess + logged ``task_cancelled_by_backend``. Let
            # the cancellation finish so the wrapper Task transitions to
            # ``cancelled`` cleanly (its ``done()`` becomes True, the
            # ``in_flight.discard`` on the next tick removes it).
            raise
        except Exception:  # noqa: BLE001 — one task's failure must not kill the loop
            logger.exception("task_execution_error", task_id=task.get("task_id"))

    for task in tasks:
        # Lift E14 — backend-initiated cancels arrive on the SAME poll
        # stream as new executes (the ExecutorAdapter XADDs the cancel
        # marker onto ``tasks:worker:{id}``). When the action is
        # ``cancel``, look the in-flight Task up by task_id and call
        # ``.cancel()`` instead of spawning a new handler. An unknown
        # task_id (cancel raced ahead of execute, or the execute already
        # finished) is a quiet no-op — the worker has nothing to abort.
        action = task.get("action") or "execute"
        if action == "cancel":
            target_task_id = task.get("task_id")
            if not target_task_id:
                logger.warning("cancel_message_missing_task_id")
                continue
            # Lift E15 — explicit per-boundary log so the cancel chain is
            # diagnosable from logs alone (the E14 dogfood failed silently
            # because no log line confirmed the worker had seen the
            # message — diagnosis required ``ps aux`` + speculation).
            logger.info("worker_poll_received_cancel", task_id=target_task_id)
            running = _RUNNING_TASKS.get(target_task_id)
            if running is None:
                logger.info(
                    "cancel_message_no_in_flight_match",
                    task_id=target_task_id,
                )
                continue
            cancelled = running.cancel()
            logger.info(
                "worker_task_cancel_started",
                task_id=target_task_id,
                asyncio_cancel_returned=cancelled,
            )
            continue
        in_flight.add(asyncio.create_task(_run(task)))
    return in_flight


# ── Main loop ─────────────────────────────────────────────────────────────────


async def _acquire_worker_token(client: httpx.AsyncClient, settings: WorkerSettings) -> str:
    """Resolve the worker token in priority order — Lift E8 Bug 4.

    Source order:

    1. ``settings.token`` (``BSVIBE_WORKER_TOKEN`` env) — wins if set.
    2. :func:`load_worker_token` — the file ``bsvibe-worker register`` writes
       to ``~/.bsvibe/worker.token`` (or ``$BSVIBE_HOME/worker.token``).
       Without this fallback, ``bsvibe-worker run`` after a successful
       ``register`` step always auto-re-registers a new worker (duplicate
       worker rows + ModelAccount rows in the workspace on every run).
    3. Auto-register only when both above are empty (requires a host OAuth
       bearer — Lift E5 removed the legacy install-token fallback).
    """
    token = settings.token
    if token:
        logger.info("worker_token_loaded", source="env")  # noqa: S106 — log label, not a secret
        return token

    saved = load_worker_token()
    if saved:
        settings.token = saved
        logger.info("worker_token_loaded", source="file")  # noqa: S106 — log label, not a secret
        return saved

    logger.info("no_worker_token", hint="registering with backend")
    bearer = _resolve_host_bearer(settings)
    if not bearer:
        raise RuntimeError("no host OAuth credential; run `bsvibe login` on this host first.")
    minted = await register(
        client,
        name=settings.name,
        bearer_token=bearer,
        capabilities=detect_capabilities(),
    )
    settings.token = minted
    _persist_worker_token(minted, settings)
    logger.info("worker_token_loaded", source="registered")  # noqa: S106 — log label, not a secret
    return minted


def _wire_executors() -> dict[str, ExecutorProtocol]:
    """Resolve capabilities → executor instances.

    Lift E12 — when ``~/.bsvibe/config.json`` declares an explicit
    capabilities list, honour it. The founder ran
    ``bsvibe-worker register --capabilities codex,opencode`` to limit the
    worker to those executors; without this filter the loop re-detects
    PATH-available CLIs (including the unwanted ``claude_code``) and
    silently broadens the worker's surface beyond the founder's choice.
    """
    persisted = load_worker_config()
    if persisted is not None and persisted.capabilities:
        capabilities = list(persisted.capabilities)
    else:
        capabilities = detect_capabilities()
    executors: dict[str, ExecutorProtocol] = {}
    for cap in capabilities:
        try:
            executors[cap] = select_executor(cap)
        except ValueError:
            # Declared but no executor wired in this lift — skip.
            logger.info("capability_declared_no_executor", capability=cap)
    return executors


async def _maybe_start_opencode_serve(
    settings: WorkerSettings,
    executors: dict[str, ExecutorProtocol],
) -> opencode_server.OpenCodeServerProcess | None:
    """Lift E17 — long-running ``opencode serve`` daemon.

    The ``opencode`` executor talks HTTP to this daemon instead of spawning
    per-task subprocesses; dogfood after E16 found per-call startup tax made
    the subprocess path 8 h wall-clock on a trivial prompt vs 2.7 s for the
    daemon path. We start serve before the poll loop begins so the first
    polled opencode task hits a ready URL, and the caller stops it in its
    ``finally`` so daemon lifetime is bounded by the worker process's.

    Returns ``None`` when opencode is not a wired capability OR when the
    daemon failed to start (logged + degraded — non-opencode tasks still run).
    """
    if "opencode" not in executors:
        return None
    try:
        daemon = await opencode_server.start_opencode_serve(settings)
    except opencode_server.OpenCodeServeStartupError:
        logger.error(
            "opencode_serve_disabled",
            hint="opencode tasks will fail until the daemon starts cleanly",
        )
        return None
    opencode_server.set_serve_url(daemon.url)
    return daemon


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
    token = await _acquire_worker_token(client, settings)
    executors = _wire_executors()
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

    opencode_daemon = await _maybe_start_opencode_serve(settings, executors)

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
        # Lift E17 — group-kill the ``opencode serve`` daemon so its child
        # processes (Bun runtime, helper workers) die alongside the worker.
        if opencode_daemon is not None:
            await opencode_server.stop_opencode_serve(opencode_daemon)
            opencode_server.clear_serve_url()


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
    """Persist the freshly minted worker token to ``~/.bsvibe/worker.token``.

    Lift E12 — the legacy CWD ``.env`` writeback is gone (it caused the
    qazasa123 cross-CWD bug where ``register`` wrote ``~/.env`` and ``run``
    from a different CWD never saw it). The canonical store is the
    XDG-style ``~/.bsvibe/`` directory; ``register`` writes the matching
    ``config.json`` directly via :func:`save_worker_config` in the CLI.
    """
    del settings  # token is the only thing persisted here now
    try:
        save_worker_token(token)
    except OSError:  # pragma: no cover — file-system failure is non-fatal
        logger.warning("worker_token_file_save_failed", exc_info=True)


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


_HOSTNAME_DEFAULT = WorkerSettings.model_fields["name"].default


def _apply_persisted_config(settings: WorkerSettings) -> WorkerSettings:
    """Layer ``~/.bsvibe/config.json`` UNDER any explicit env override — Lift E12.

    Source priority for each field — first non-empty wins:

    1. ``BSVIBE_WORKER_*`` env var (already loaded into ``settings``).
    2. ``~/.bsvibe/config.json`` (the canonical persisted source from
       ``bsvibe-worker register``).
    3. Hard-coded defaults (``socket.gethostname()`` for ``name``,
       :func:`detect_capabilities` for capabilities, ``http://localhost:8400``
       for ``server_url``).

    Logs a structured ``worker_config_loaded`` line with the resolved source
    for each field so future debugging is cheap.
    """
    persisted = load_worker_config()
    sources = {
        "name": "env" if settings.name != _HOSTNAME_DEFAULT else "default",
        "server_url": (
            "env"
            if settings.server_url != WorkerSettings.model_fields["server_url"].default
            else "default"
        ),
        "capabilities": "default",
        "labels": "default",
    }
    if persisted is not None:
        if sources["name"] == "default" and persisted.name:
            settings.name = persisted.name
            sources["name"] = "file"
        if sources["server_url"] == "default" and persisted.server_url:
            settings.server_url = persisted.server_url
            sources["server_url"] = "file"
        if persisted.capabilities:
            sources["capabilities"] = "file"
        if persisted.labels:
            sources["labels"] = "file"
    logger.info("worker_config_loaded", sources=sources)
    return settings


def _ensure_process_group() -> None:
    """Lift E14 — make the worker daemon its own process group leader.

    When the worker spawns ``claude --print`` / ``codex -p`` / ``opencode run``
    subprocesses, they inherit the daemon's process group. If the daemon
    dies abruptly (SIGKILL, segfault, uncaught exception before our
    asyncio cancel paths fire) the CLI subprocesses survive as orphans —
    the dogfood found 7 of these alive 22 h after their parent worker
    daemon's death.

    Making the daemon a process group leader lets a future "nuke
    everything" path (or an OS-level supervisor like systemd or
    ``launchd`` with ``KillMode=control-group``) terminate the whole
    group atomically. Guarded by ``sys.platform`` — POSIX-only; the
    worker runs only on Mac/Linux per the design.
    """
    if sys.platform == "win32":  # pragma: no cover — workers don't run on Windows
        return
    try:
        os.setpgrp()
    except OSError:  # pragma: no cover — only fails if we're already a session leader
        logger.debug("setpgrp_skipped", reason="already_pg_leader_or_unsupported")


def _cancel_all_running_tasks() -> None:
    """Lift E14 — cancel every task in :data:`_RUNNING_TASKS`.

    Called from the SIGINT/SIGTERM signal handler so a shutdown
    propagates IMMEDIATELY into the in-flight task cancellation —
    without this, the poll loop's own ``stop.set()`` only takes effect
    on the NEXT iteration of ``_interruptible_sleep``, leaving a slow
    handler running for up to ``poll_interval_seconds``. Cancelling the
    tasks here ensures each running streaming executor's ``finally``
    block fires (killing the CLI subprocess) within the same event-loop
    tick the signal arrives on.
    """
    for task_id, running in list(_RUNNING_TASKS.items()):
        if not running.done():
            logger.info("worker_shutdown_cancel_in_flight", task_id=task_id)
            running.cancel()


async def _amain() -> None:
    _ensure_process_group()
    settings = _apply_persisted_config(get_worker_settings())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _shutdown_handler() -> None:
        stop.set()
        _cancel_all_running_tasks()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_handler)
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
