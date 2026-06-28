"""Dispatch substrate for the executor pool (Lift 2 of the executor-pool epic).

Ports BSGateway's dispatch path (``executor/dispatcher.py`` +
``chat/service.py::_execute_via_worker`` / ``_await_task_completion``) to async
SQLAlchemy + ``workspace_id``. This is the **substrate only** — no real CLI /
worker process (Lift 3) and no run-path integration (Lift 5).

The contract (matching BSGateway):

* The ``executor_tasks`` **DB row is the source of truth**. A task is created
  ``pending``; dispatch XADDs a notification onto the worker's dedicated stream
  (``tasks:worker:{worker_id}``) and flips the row to ``dispatched``; the worker
  later reports a result that flips the row to ``done`` / ``failed``.
* Completion is signalled on the ``task:{id}:done`` pub/sub channel — but the
  awaiter always re-reads the DB row for the canonical terminal state, with a
  **DB fallback** (a single read) when the publish is missed, and a typed
  :class:`TaskTimeout` when nothing arrives in time.
* Redis Stream fields must be **flat strings** (the XADD constraint), so the
  task payload is built entirely from ``str`` values.

The injected ``redis`` client is any ``redis.asyncio.Redis`` (or compatible
fake) configured with ``decode_responses=True``; callers own the SQLAlchemy
session transaction boundary (these functions ``add`` / ``flush`` but never
``commit``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.executors.db import ExecutorTaskRow, WorkerRow
from backend.storage.artifact_store import ArtifactStore, LocalFilesystemArtifactStore

logger = structlog.get_logger(__name__)

# A worker is "available" only if it heart-beat within this window (mirrors
# BSGateway's ``find_available_worker`` ``INTERVAL '120 seconds'``).
HEARTBEAT_FRESHNESS_S = 120


def is_heartbeat_fresh(last_heartbeat: datetime | None) -> bool:
    """Lift E13 — the same predicate :func:`find_available_worker` uses.

    Exposed so the REST + MCP worker-list surfaces can surface a
    ``heartbeat_fresh`` field without duplicating the cutoff calc. A
    ``status="online"`` worker whose heartbeat is older than the cutoff
    is a real diagnosis — the daemon died before clearing the column.
    """
    if last_heartbeat is None:
        return False
    # Tolerate naive timestamps (legacy SQLite rows) by stamping UTC.
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_heartbeat).total_seconds() <= HEARTBEAT_FRESHNESS_S


WORKER_STREAM_PREFIX = "tasks:worker:"

_TERMINAL_STATUSES = ("done", "failed")

# How often :func:`await_completion` re-reads the DB row as a safety net when no
# done-channel signal arrives (a remote worker reporting over HTTP whose publish
# was somehow missed). Short enough that a missed signal resolves in seconds, not
# at ``timeout_s`` (which is the executor-task timeout, ~1800s by default).
_AWAIT_POLL_INTERVAL_S = 2.0


class TaskTimeout(Exception):
    """Raised when :func:`await_completion` sees no terminal result in time."""


class _RedisDispatch(Protocol):
    """The narrow Redis surface the dispatch substrate needs.

    Any ``redis.asyncio.Redis`` (or ``fakeredis.aioredis.FakeRedis``) satisfies
    it; narrowed so callers may inject a fake freely. ``decode_responses=True``
    is assumed so stream/pubsub payloads are ``str``.
    """

    async def xadd(self, name: str, fields: dict[str, Any], **kwargs: Any) -> Any: ...

    async def publish(self, channel: str, message: str) -> Any: ...

    def pubsub(self) -> Any: ...


# ── channel / stream naming (stable identifiers shared with the worker) ───────


def worker_stream(worker_id: uuid.UUID) -> str:
    """The worker's dedicated dispatch stream (one XADD per task)."""
    return f"{WORKER_STREAM_PREFIX}{worker_id}"


def stream_channel(task_id: uuid.UUID) -> str:
    """Pub/sub channel the worker publishes incremental output chunks to."""
    return f"task:{task_id}:stream"


def done_channel(task_id: uuid.UUID) -> str:
    """Pub/sub channel the worker publishes the terminal completion signal to."""
    return f"task:{task_id}:done"


# ── worker selection ──────────────────────────────────────────────────────────


async def find_available_worker(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    executor_type: str,
    pinned_worker_id: uuid.UUID | None = None,
    max_parallel_per_worker: int | None = None,
) -> WorkerRow | None:
    """Return an online, capability-matching worker for ``workspace_id``, or ``None``.

    A worker is eligible when it is active + ``status="online"`` + heart-beat
    within :data:`HEARTBEAT_FRESHNESS_S` + its ``capabilities`` contain
    ``executor_type``. The least-recently-pinged eligible worker wins (cheap
    round-robin, matching BSGateway's ``ORDER BY last_heartbeat ASC``).

    ``pinned_worker_id`` (optional) is honoured — the caller explicitly bound
    this worker — as long as it is active + in the workspace + carries the
    capability + **its heartbeat is FRESH**. A pinned id that doesn't qualify
    (wrong capability, or a STALE/dead heartbeat) falls through to the normal
    availability scan, so work lands on another live worker instead of stalling
    on a dead pin. The freshness gate matters because the executor model account
    pins ``extra_params.worker_id`` at registration; when that machine is
    replaced or dies, the pin goes stale and a task dispatched there is never
    polled — the run silently waits out the dispatch timeout. **A FRESH pinned
    worker is ALSO accepted when saturated** (``last_in_flight >=
    max_parallel_per_worker``): the founder pinned a specific machine for a
    reason and the waiting belongs in
    :class:`~backend.dispatch.adapter.ExecutorAdapter.chat`, not here.

    ``max_parallel_per_worker`` (Lift E16, optional) — when set, eligible
    rows whose last-reported ``last_in_flight`` reached this cap are
    EXCLUDED. The backend used to dispatch onto a saturated worker's
    stream and start its 600 s ``await_completion`` timer; the worker's
    poll loop skips polling at-cap, so the timer expired while the task
    was never read — chunks were marked ``failed`` before they ran. The
    capacity-exclusion stops that. ``None`` keeps the pre-E16 behaviour
    (no capacity gate at all). A ``last_in_flight`` of ``NULL`` is treated
    as "no signal — let it through" for back-compat with pre-E16 workers
    that don't report a count.

    On capacity exhaustion logs ``find_available_worker_all_saturated``
    with a count of capability-matching workers and saturated workers —
    future capacity diagnostics need a signal that this code path fired.
    """
    if pinned_worker_id is not None:
        pinned = (
            await session.execute(
                select(WorkerRow).where(
                    WorkerRow.id == pinned_worker_id,
                    WorkerRow.workspace_id == workspace_id,
                    WorkerRow.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if (
            pinned is not None
            and executor_type in (pinned.capabilities or [])
            and is_heartbeat_fresh(pinned.last_heartbeat)
        ):
            return pinned

    cutoff = datetime.now(UTC).timestamp() - HEARTBEAT_FRESHNESS_S
    rows = (
        (
            await session.execute(
                select(WorkerRow)
                .where(
                    WorkerRow.workspace_id == workspace_id,
                    WorkerRow.is_active.is_(True),
                    WorkerRow.status == "online",
                    WorkerRow.last_heartbeat.is_not(None),
                )
                .order_by(WorkerRow.last_heartbeat.asc())
            )
        )
        .scalars()
        .all()
    )
    total_eligible = 0
    saturated = 0
    for row in rows:
        if row.last_heartbeat is None:
            continue
        # JSON ``capabilities`` is best-matched in Python — keeps the query
        # portable across SQLite (tests) and Postgres without a JSON operator.
        if executor_type not in (row.capabilities or []):
            continue
        last = row.last_heartbeat
        # SQLite returns naive datetimes; treat them as UTC for the comparison.
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last.timestamp() < cutoff:
            continue
        total_eligible += 1
        # Lift E16 capacity gate. A NULL count is "no signal — let it
        # through" (pre-E16 worker that never reported); a positive count
        # at-or-over the cap excludes the row.
        if (
            max_parallel_per_worker is not None
            and row.last_in_flight is not None
            and row.last_in_flight >= max_parallel_per_worker
        ):
            saturated += 1
            continue
        return row
    # Lift E16 — explicit log when capacity exclusion is the reason we
    # return None. Distinguishes "all-saturated" from "no worker at all"
    # so a future founder dogfood debug session sees the signal in logs
    # without needing a redis introspection.
    if max_parallel_per_worker is not None and total_eligible > 0 and saturated == total_eligible:
        logger.info(
            "find_available_worker_all_saturated",
            workspace_id=str(workspace_id),
            executor_type=executor_type,
            total_eligible=total_eligible,
            saturated=saturated,
            max_parallel_per_worker=max_parallel_per_worker,
        )
    return None


# ── task lifecycle ────────────────────────────────────────────────────────────


async def create_task(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    executor_type: str,
    prompt: str,
    system: str = "",
    workspace_dir: str = ".",
    run_id: uuid.UUID | None = None,
    model: str | None = None,
    repo_url: str | None = None,
) -> ExecutorTaskRow:
    """Create a ``pending`` :class:`ExecutorTaskRow` and flush it (no commit).

    ``run_id`` (optional) binds the task to its :class:`ExecutionRun` so the
    result path can resolve the run workspace to persist captured files into
    (executor-pool B1). It is nullable: substrate-only callers omit it.

    ``model`` (optional, Lift E21) is the underlying LLM model id the worker
    forwards to the executor (e.g. ``opencode-go/qwen3.6-plus``). NULL means
    "use the CLI's default model" — the back-compat shape pre-E21.

    ``repo_url`` (optional, Lift E32) — when set, the worker shallow-clones
    this git URL into the per-task workspace BEFORE invoking the executor so
    the coding agent has real files to read + edit. NULL keeps the pre-E32
    empty-tempdir behaviour for chat-shaped callers.
    """
    task = ExecutorTaskRow(
        workspace_id=workspace_id,
        run_id=run_id,
        executor_type=executor_type,
        prompt=prompt,
        system=system,
        workspace_dir=workspace_dir,
        model=model,
        repo_url=repo_url,
        status="pending",
    )
    session.add(task)
    await session.flush()
    logger.info(
        "executor_task_created",
        task_id=str(task.id),
        workspace_id=str(workspace_id),
        executor_type=executor_type,
    )
    return task


async def dispatch_task(
    redis: _RedisDispatch,
    *,
    session: AsyncSession,
    task: ExecutorTaskRow,
    worker_id: uuid.UUID,
) -> str:
    """XADD ``task`` onto the worker's stream + mark it ``dispatched``.

    The payload is flat strings only (the Redis Streams constraint). The DB row
    is flipped to ``status="dispatched"`` with ``worker_id`` set in the SAME
    session (the caller commits). Returns the stream entry id.
    """
    payload: dict[str, Any] = {
        "task_id": str(task.id),
        "executor_type": task.executor_type,
        "prompt": task.prompt,
        "system": task.system,
        "workspace_dir": task.workspace_dir,
        "stream_channel": stream_channel(task.id),
        "done_channel": done_channel(task.id),
        "action": "execute",
        "dispatched_at": datetime.now(UTC).isoformat(),
    }
    # Lift E21 — forward the underlying model id only when set. Redis Streams
    # reject ``None``; omit the key entirely so the worker's
    # ``task.get("model") or None`` keeps the back-compat "use CLI default"
    # path for legacy callers that have not started passing the model.
    if task.model:
        payload["model"] = task.model
    # Lift E32 — when the dispatch is for a coding-agent run, ship the
    # repo URL so the worker shallow-clones it into the per-task workspace
    # before calling the executor. Same omit-when-empty rule as ``model``.
    if task.repo_url:
        payload["repo_url"] = task.repo_url
    msg_id = await redis.xadd(worker_stream(worker_id), payload)

    task.worker_id = worker_id
    task.status = "dispatched"
    await session.flush()
    logger.info(
        "executor_task_dispatched",
        task_id=str(task.id),
        worker_id=str(worker_id),
        executor_type=task.executor_type,
    )
    return str(msg_id)


async def cancel_task(
    redis: _RedisDispatch,
    *,
    worker_id: uuid.UUID,
    task_id: uuid.UUID,
) -> None:
    """XADD a cancel marker onto ``worker_id``'s stream — Lift E14.

    When :class:`~backend.dispatch.adapter.ExecutorAdapter.chat` times out
    waiting on a worker, the worker is still happily running the
    ``opencode run`` / ``claude --print`` / ``codex -p`` subprocess for
    many more minutes — wasting compute + holding a parallel-task slot —
    because the result POST path is the only way the worker learns the
    backend gave up. This function notifies the worker by reusing the
    same dispatch stream the original task came in on: the worker's poll
    loop pulls the cancel marker, looks the task_id up in its in-flight
    subprocess registry, and terminates it.

    Best-effort: a redis hiccup is logged + swallowed. The backend has
    already moved on from the task (raised :class:`TaskTimeout` to its
    own caller) so an unsuccessful cancel costs only the worker's
    compute, not data integrity.
    """
    # Lift E15 — explicit pre-XADD log so the cancel chain is diagnosable
    # from logs alone (E14 dogfood: zero log evidence of cancel propagation
    # made diagnosis painful). Logged BEFORE the redis call so a redis
    # failure still produces a "we tried" record.
    logger.info(
        "dispatch_cancel_task_called",
        task_id=str(task_id),
        worker_id=str(worker_id),
        stream=worker_stream(worker_id),
    )
    payload: dict[str, Any] = {
        "task_id": str(task_id),
        "action": "cancel",
        "dispatched_at": datetime.now(UTC).isoformat(),
    }
    try:
        await redis.xadd(worker_stream(worker_id), payload)
    except Exception:  # noqa: BLE001 — cancel is best-effort, not a control-flow gate
        logger.warning(
            "executor_cancel_publish_failed",
            task_id=str(task_id),
            worker_id=str(worker_id),
            exc_info=True,
        )
        return
    logger.info(
        "dispatch_cancel_xadd_succeeded",
        task_id=str(task_id),
        worker_id=str(worker_id),
        stream=worker_stream(worker_id),
    )


def _persist_task_files(
    *,
    run_id: uuid.UUID,
    store: ArtifactStore,
    files: list[dict[str, Any]],
) -> list[str]:
    """Persist worker-returned files into ``store`` under the run (B1).

    The traversal guard is now CENTRALIZED in
    :class:`~backend.storage.artifact_store.LocalFilesystemArtifactStore` (a
    ``../`` ref raises :class:`ValueError`). Truncation-marker entries
    (``truncated: True``, empty content) are recorded as refs but written
    empty. Returns the accepted relative paths (the recorded
    ``artifact_refs``); a rejected / malformed entry is skipped, never
    written — same observable behaviour as the pre-lift inline implementation.
    """
    accepted: list[str] = []
    for entry in files:
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            logger.warning("artifact_persist_skipped_no_path", run_id=str(run_id))
            continue
        truncated = bool(entry.get("truncated"))
        try:
            raw = b"" if truncated else base64.b64decode(entry.get("content_b64") or "")
        except (binascii.Error, ValueError):
            logger.warning("artifact_persist_bad_base64", run_id=str(run_id), ref=rel)
            continue
        try:
            store.put(run_id, rel, raw)
        except ValueError:
            # Traversal / absolute-path refs are refused by the store's
            # centralized guard. Skip + log; never written, never recorded.
            logger.warning("artifact_persist_rejected_traversal", run_id=str(run_id), ref=rel)
            continue
        accepted.append(rel)
    return accepted


async def record_result(
    session: AsyncSession,
    redis: _RedisDispatch,
    *,
    task_id: uuid.UUID,
    success: bool,
    output: str,
    error_message: str | None,
    files: list[dict[str, Any]] | None = None,
    run_workspace_root: str | None = None,
    artifact_store: ArtifactStore | None = None,
) -> ExecutorTaskRow | None:
    """Close a task ``done`` / ``failed`` from a worker result. ``None`` if unknown.

    B1: when the worker ships ``files`` (each ``{path, content_b64, truncated}``)
    and the task carries a ``run_id``, they are persisted into ``artifact_store``
    (the per-run :class:`~backend.storage.artifact_store.ArtifactStore` seam) and
    their accepted relative paths recorded on ``task.artifact_refs`` — so the
    existing artifact-read endpoint serves them. A task with ``run_id is None``
    skips persistence (back-compat).

    ``artifact_store`` wins when provided (production passes the singleton from
    deps). For back-compat with existing callers / tests that pass
    ``run_workspace_root`` as a string, a
    :class:`~backend.storage.artifact_store.LocalFilesystemArtifactStore` is
    built on the fly from that root (or ``settings.run_workspace_root`` when
    nothing is given). The Protocol-level traversal guard (centralized in the
    store) replaces the inline ``is_relative_to`` check.

    After the DB row flips terminal, PUBLISH the :func:`done_channel` signal on
    ``redis``. This is the **authoritative** completion signal: a remote worker
    reaches the backend only over HTTP (``POST /api/v1/workers/result``) and
    usually has no redis to publish from, so the backend — which owns redis —
    publishes here so any :func:`await_completion` wakes promptly instead of
    blocking until its timeout. A worker that also has redis publishing the same
    channel is harmless (idempotent wake). The publish is best-effort: a pub/sub
    hiccup must not roll back the recorded result.
    """
    task = await session.get(ExecutorTaskRow, task_id)
    if task is None:
        return None
    task.status = "done" if success else "failed"
    task.output = output
    task.error_message = error_message

    # Persist captured files + record real artifact_refs (B1). Skipped when the
    # task has no run binding (substrate-only / back-compat) or no files shipped.
    if files and task.run_id is not None:
        store = artifact_store or LocalFilesystemArtifactStore(
            Path(run_workspace_root or get_settings().run_workspace_root)
        )
        accepted = await asyncio.to_thread(
            _persist_task_files,
            run_id=task.run_id,
            store=store,
            files=files,
        )
        task.artifact_refs = accepted
        logger.info(
            "executor_task_artifacts_persisted",
            task_id=str(task_id),
            run_id=str(task.run_id),
            count=len(accepted),
        )

    await session.flush()
    logger.info(
        "executor_task_result_recorded",
        task_id=str(task_id),
        status=task.status,
    )
    try:
        await redis.publish(done_channel(task_id), json.dumps({"task_id": str(task_id)}))
    except Exception:  # noqa: BLE001 — publish is a wake hint, the DB row is truth
        logger.warning("executor_result_publish_failed", task_id=str(task_id), exc_info=True)
    return task


async def _read_terminal(session: AsyncSession, task_id: uuid.UUID) -> ExecutorTaskRow | None:
    """Re-read ``task_id`` and return it iff it is in a terminal state."""
    # ``session.get`` would serve a stale identity-map copy when the writer used
    # a different session; a fresh SELECT reflects the committed terminal row.
    row = (
        await session.execute(select(ExecutorTaskRow).where(ExecutorTaskRow.id == task_id))
    ).scalar_one_or_none()
    if row is not None:
        await session.refresh(row)
    if row is not None and row.status in _TERMINAL_STATUSES:
        return row
    return None


async def await_completion(
    redis: _RedisDispatch,
    *,
    session: AsyncSession,
    task_id: uuid.UUID,
    timeout_s: float,
) -> ExecutorTaskRow:
    """Wait for ``task:{id}:done``, with a periodic DB poll as a safety net.

    Subscribes to the done channel for the fast path (wake immediately on a
    published signal), but also re-reads the DB row every
    :data:`_AWAIT_POLL_INTERVAL_S` seconds so a **missed** signal — the common
    case for a remote worker that reports its result over HTTP and cannot
    publish — still resolves within the poll interval rather than blocking until
    ``timeout_s``. Either path returns the row once it is terminal. Raises
    :class:`TaskTimeout` only if the row never becomes terminal within
    ``timeout_s``.
    """
    # Fast path: the result may already be terminal (worker beat the awaiter).
    early = await _read_terminal(session, task_id)
    if early is not None:
        return early

    chan = done_channel(task_id)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(chan)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            # Cap each wait at the poll interval so the DB safety-net read fires
            # on a short cadence even when no done message ever arrives. A
            # published signal still wakes us early (get_message returns at once).
            poll_wait = min(_AWAIT_POLL_INTERVAL_S, remaining)
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_wait),
                    timeout=poll_wait + 0.5,
                )
            except TimeoutError:
                msg = None
            # Whether or not a signal arrived, re-read the row: the signal is the
            # fast path; the periodic read is the safety net for a missed publish.
            _ = msg
            row = await _read_terminal(session, task_id)
            if row is not None:
                return row
    except Exception:  # noqa: BLE001 — a pub/sub hiccup degrades to the DB poll
        logger.warning("executor_await_pubsub_failed", task_id=str(task_id), exc_info=True)
        # Degrade to a pure DB poll for the remaining budget.
        row = await _poll_until_terminal(session, task_id, timeout_s=timeout_s)
        if row is not None:
            return row
    finally:
        try:
            await pubsub.unsubscribe(chan)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — cleanup best-effort
            logger.debug("executor_await_pubsub_close_failed", task_id=str(task_id))

    # Final read in case the row turned terminal between the last poll and the
    # deadline / a pub/sub teardown.
    row = await _read_terminal(session, task_id)
    if row is not None:
        return row
    raise TaskTimeout(f"executor task {task_id} did not complete within {timeout_s}s")


async def _poll_until_terminal(
    session: AsyncSession, task_id: uuid.UUID, *, timeout_s: float
) -> ExecutorTaskRow | None:
    """Pure DB poll fallback used when pub/sub is unavailable.

    Re-reads the row every :data:`_AWAIT_POLL_INTERVAL_S` until terminal or the
    deadline passes. Returns the terminal row, or ``None`` on timeout (the caller
    raises :class:`TaskTimeout`).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        row = await _read_terminal(session, task_id)
        if row is not None:
            return row
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_AWAIT_POLL_INTERVAL_S, remaining))
    return await _read_terminal(session, task_id)


async def mark_pending(session: AsyncSession, *, task_id: uuid.UUID) -> None:
    """Reset a task to ``pending`` (e.g. dispatch rolled back). Idempotent."""
    await session.execute(
        update(ExecutorTaskRow)
        .where(ExecutorTaskRow.id == task_id)
        .values(status="pending", worker_id=None)
    )
    await session.flush()


__all__ = [
    "HEARTBEAT_FRESHNESS_S",
    "TaskTimeout",
    "await_completion",
    "cancel_task",
    "create_task",
    "dispatch_task",
    "done_channel",
    "find_available_worker",
    "is_heartbeat_fresh",
    "mark_pending",
    "record_result",
    "stream_channel",
    "worker_stream",
]
