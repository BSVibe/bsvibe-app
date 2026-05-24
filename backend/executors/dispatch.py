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
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.executors.db import ExecutorTaskRow, WorkerRow

logger = structlog.get_logger(__name__)

# A worker is "available" only if it heart-beat within this window (mirrors
# BSGateway's ``find_available_worker`` ``INTERVAL '120 seconds'``).
HEARTBEAT_FRESHNESS_S = 120

WORKER_STREAM_PREFIX = "tasks:worker:"

_TERMINAL_STATUSES = ("done", "failed")


class TaskTimeout(Exception):
    """Raised when :func:`await_completion` sees no terminal result in time."""


class _RedisDispatch(Protocol):
    """The narrow Redis surface the dispatch substrate needs.

    Any ``redis.asyncio.Redis`` (or ``fakeredis.aioredis.FakeRedis``) satisfies
    it; narrowed so callers may inject a fake freely. ``decode_responses=True``
    is assumed so stream/pubsub payloads are ``str``.
    """

    async def xadd(self, name: str, fields: dict[str, Any], **kwargs: Any) -> Any: ...

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
) -> WorkerRow | None:
    """Return an online, capability-matching worker for ``workspace_id``, or ``None``.

    A worker is eligible when it is active + ``status="online"`` + heart-beat
    within :data:`HEARTBEAT_FRESHNESS_S` + its ``capabilities`` contain
    ``executor_type``. The least-recently-pinged eligible worker wins (cheap
    round-robin, matching BSGateway's ``ORDER BY last_heartbeat ASC``).

    ``pinned_worker_id`` (optional) is accepted even with a stale heartbeat —
    the caller explicitly bound this worker — as long as it is active + in the
    workspace + carries the capability. A pinned id that doesn't qualify falls
    through to the normal availability scan.
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
        if pinned is not None and executor_type in (pinned.capabilities or []):
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
        if last.timestamp() >= cutoff:
            return row
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
) -> ExecutorTaskRow:
    """Create a ``pending`` :class:`ExecutorTaskRow` and flush it (no commit)."""
    task = ExecutorTaskRow(
        workspace_id=workspace_id,
        executor_type=executor_type,
        prompt=prompt,
        system=system,
        workspace_dir=workspace_dir,
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


async def record_result(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    success: bool,
    output: str,
    error_message: str | None,
) -> ExecutorTaskRow | None:
    """Close a task ``done`` / ``failed`` from a worker result. ``None`` if unknown."""
    task = await session.get(ExecutorTaskRow, task_id)
    if task is None:
        return None
    task.status = "done" if success else "failed"
    task.output = output
    task.error_message = error_message
    await session.flush()
    logger.info(
        "executor_task_result_recorded",
        task_id=str(task_id),
        status=task.status,
    )
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
    """Wait for ``task:{id}:done`` then read the terminal DB row.

    Subscribes to the done channel and, on each message (or an immediate
    already-terminal check), re-reads the row; returns it once terminal. On a
    missed publish a single **DB fallback** read still returns a row that became
    terminal meanwhile. Raises :class:`TaskTimeout` when nothing terminal
    appears within ``timeout_s``.
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
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining,
                )
            except TimeoutError:
                break
            if msg is None:
                # No message this poll — keep waiting until the deadline.
                continue
            row = await _read_terminal(session, task_id)
            if row is not None:
                return row
    except Exception:  # noqa: BLE001 — a pub/sub hiccup degrades to the DB fallback
        logger.warning("executor_await_pubsub_failed", task_id=str(task_id), exc_info=True)
    finally:
        try:
            await pubsub.unsubscribe(chan)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001 — cleanup best-effort
            logger.debug("executor_await_pubsub_close_failed", task_id=str(task_id))

    # Fallback: one final DB read in case the publish was missed entirely.
    row = await _read_terminal(session, task_id)
    if row is not None:
        return row
    raise TaskTimeout(f"executor task {task_id} did not complete within {timeout_s}s")


async def claim_pending_task(session: AsyncSession, *, limit: int = 1) -> ExecutorTaskRow | None:
    """Return the oldest ``pending`` task (FIFO), or ``None`` — for the worker.

    A thin helper the :class:`backend.workers.executor_dispatch.ExecutorDispatchWorker`
    uses to find work to dispatch. ``limit`` is reserved for a future batch tick.
    """
    _ = limit
    return (
        await session.execute(
            select(ExecutorTaskRow)
            .where(ExecutorTaskRow.status == "pending")
            .order_by(ExecutorTaskRow.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


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
    "claim_pending_task",
    "create_task",
    "dispatch_task",
    "done_channel",
    "find_available_worker",
    "mark_pending",
    "record_result",
    "stream_channel",
    "worker_stream",
]
