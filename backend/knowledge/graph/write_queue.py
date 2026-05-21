"""SQLite write queue — Sprint 3 (S3-4 / G4).

SQLite has a global write lock per database file, so concurrent
writers from multiple asyncio tasks contend on the same connection
and surface as ``database is locked`` errors. The fix audited in
``BSVibe_Ecosystem_Audit.md §7.5`` is a **single writer task fed by
an asyncio.Queue** — every write op is enqueued, the writer drains
it serially, and reads stay independent.

Public API
----------

``SQLiteWriteQueue.submit(op)``
    Enqueue an awaitable factory; awaits backpressure when the queue
    is full and resolves to the op's return value (or
    :class:`WriteOpFailedError`).

``SQLiteWriteQueue.try_submit(op)``
    Non-blocking variant — raises :class:`QueueFullError` immediately
    when the queue is full. Returns the in-flight Future so the
    caller can ``await`` it (or wrap it in a timeout) without holding
    queue capacity.

``SQLiteWriteQueue.start()`` / ``stop()``
    Lifecycle hooks for FastAPI ``lifespan``. ``stop()`` drains all
    enqueued ops before returning so no in-flight write is lost.

Backpressure
------------

``submit`` honors ``maxsize`` via :py:meth:`asyncio.Queue.put`, which
awaits when the queue is full. Concurrent submits each have their
own future and a per-call ``put`` coroutine, so there is no shared
mutable state between submitters. ``try_submit`` is the fail-fast
sibling for callers that prefer to reject over block.

Crash handling
--------------

The writer is supervised with a ``add_done_callback``. If it exits
for any reason other than an explicit ``stop()``, the supervisor
restarts it. Any in-flight ops the dead writer never resolved are
failed with :class:`WriteOpFailedError` so callers don't hang.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import structlog

logger = structlog.get_logger(__name__)


T = TypeVar("T")
WriteOp = Callable[[], Awaitable[T]]


class QueueFullError(RuntimeError):
    """Raised by :meth:`SQLiteWriteQueue.try_submit` when the queue is full."""


class QueueClosedError(RuntimeError):
    """Raised when a submit happens after :meth:`SQLiteWriteQueue.stop`."""


class WriteOpFailedError(RuntimeError):
    """Raised by :meth:`SQLiteWriteQueue.submit` when the op itself raised.

    The underlying exception is preserved as ``__cause__``.
    """


# Sentinel passed through the queue to wake the writer for shutdown.
_STOP = object()


@dataclass
class _PendingOp:
    op: WriteOp
    future: asyncio.Future = field(repr=False)


class SQLiteWriteQueue:
    """Single-writer asyncio queue for serializing SQLite write ops.

    The queue owns one background ``asyncio.Task`` (the *writer*).
    All callers enqueue closures + Futures; the writer awaits them in
    submission order and resolves the matching Future. Reads are
    NOT routed through this queue — the consumer is expected to keep
    using its existing read connection / pool concurrently.

    Parameters
    ----------
    connection
        Stored only as an opaque handle for logs / introspection. The
        queue itself never touches the connection — the closures
        passed to :meth:`submit` do.
    name
        Logging tag (``"graph"``, ``"vector"``, etc.).
    maxsize
        Max queued (not yet executing) ops. ``submit`` awaits when the
        queue is at capacity; ``try_submit`` raises
        :class:`QueueFullError`.
    """

    def __init__(
        self,
        connection: Any,
        *,
        name: str = "sqlite",
        maxsize: int = 256,
    ) -> None:
        self._connection = connection
        self._name = name
        self._maxsize = maxsize
        self._queue: asyncio.Queue[_PendingOp | object] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closed: bool = False
        self._stopping: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the writer task. Idempotent."""
        if self._writer_task is not None:
            return
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        self._closed = False
        self._stopping = False
        self._spawn_writer()
        logger.info("write_queue_started", queue=self._name, maxsize=self._maxsize)

    async def stop(self) -> None:
        """Drain pending ops then stop the writer. Safe to call twice."""
        if self._closed:
            return
        self._stopping = True
        # Tell the writer to drain & exit.
        if self._queue is not None:
            await self._queue.put(_STOP)
        # Wait for writer to finish (drain in progress).
        if self._writer_task is not None:
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Writer crashed during shutdown; we still want to mark
                # closed so further submits fail fast.
                logger.error(
                    "write_queue_stop_writer_error",
                    queue=self._name,
                    exc_info=True,
                )
        self._closed = True
        self._writer_task = None
        logger.info("write_queue_stopped", queue=self._name)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(self, op: WriteOp[T]) -> T:
        """Enqueue ``op`` and await its result.

        Awaits when the queue is full (backpressure). Raises
        :class:`QueueClosedError` if the queue has been stopped.
        Raises :class:`WriteOpFailedError` if the op itself raises.
        """
        pending = self._make_pending(op)
        # Backpressure happens here — asyncio.Queue.put awaits when full.
        # Each submitter has its own coroutine, so there is no shared
        # ``_pending_put`` state to race.
        assert self._queue is not None
        await self._queue.put(pending)
        return await pending.future

    async def try_submit(self, op: WriteOp[T]) -> asyncio.Future[T]:
        """Enqueue ``op`` without blocking; raise if the queue is full.

        Returns the in-flight Future so the caller can ``await`` it
        (or wrap it in a timeout) without holding queue capacity.
        """
        pending = self._make_pending(op)
        assert self._queue is not None
        try:
            self._queue.put_nowait(pending)
        except asyncio.QueueFull as exc:
            # Roll back the future so callers don't hold a dangling
            # reference that will never be resolved.
            if not pending.future.done():
                pending.future.cancel()
            raise QueueFullError(
                f"write queue '{self._name}' is full (maxsize={self._maxsize})"
            ) from exc
        return pending.future

    def _make_pending(self, op: WriteOp[T]) -> _PendingOp:
        if self._closed or self._queue is None:
            raise QueueClosedError(f"write queue '{self._name}' is closed")
        if self._stopping:
            raise QueueClosedError(f"write queue '{self._name}' is stopping")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        return _PendingOp(op=op, future=future)

    # ------------------------------------------------------------------
    # Internal — writer + supervisor
    # ------------------------------------------------------------------

    def _spawn_writer(self) -> None:
        task = asyncio.create_task(
            self._writer_loop(),
            name=f"sqlite-write-queue-{self._name}",
        )
        self._writer_task = task
        # `add_done_callback` runs synchronously on the event loop when
        # the writer task completes. This avoids a separate supervisor
        # task — which itself would have to be cleaned up — and instead
        # restarts the writer purely reactively.
        task.add_done_callback(self._on_writer_done)

    def _on_writer_done(self, task: asyncio.Task[None]) -> None:
        # Stop path or queue closed: nothing to do.
        if self._closed or self._stopping:
            return
        # Writer finished cleanly without a stop sentinel — that should
        # never happen, but treat it as a crash so callers don't hang.
        if task.cancelled():
            reason = "writer task cancelled"
        else:
            exc = task.exception()
            reason = (
                f"writer task crashed: {exc}"
                if exc is not None
                else ("writer task exited unexpectedly")
            )
        logger.error(
            "write_queue_writer_died_restarting",
            queue=self._name,
            reason=reason,
        )
        # Fail any pending ops the dead writer never picked up so callers
        # don't wait forever on resolved-but-orphaned futures.
        self._fail_pending_ops(reason)
        # Re-spawn the writer.
        self._spawn_writer()

    async def _writer_loop(self) -> None:
        """Drain ops one at a time. Exits on the ``_STOP`` sentinel."""
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is _STOP:
                # Drain any remaining ops (graceful shutdown).
                await self._drain_remaining()
                return
            assert isinstance(item, _PendingOp)
            await self._run_one(item)

    async def _drain_remaining(self) -> None:
        assert self._queue is not None
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STOP:
                continue
            assert isinstance(item, _PendingOp)
            await self._run_one(item)

    async def _run_one(self, pending: _PendingOp) -> None:
        if pending.future.cancelled():
            return
        try:
            result = await pending.op()
        except asyncio.CancelledError:
            # Writer was cancelled mid-flight (crash path). Surface the
            # cancellation as ``WriteOpFailedError`` so the caller does
            # not hang on a never-resolved future, then re-raise so the
            # writer task itself exits.
            if not pending.future.done():
                pending.future.set_exception(WriteOpFailedError("writer task cancelled mid-flight"))
            raise
        except Exception as exc:
            logger.warning(
                "write_queue_op_failed",
                queue=self._name,
                error=str(exc),
                exc_info=True,
            )
            if not pending.future.done():
                wrapped = WriteOpFailedError(str(exc))
                wrapped.__cause__ = exc
                pending.future.set_exception(wrapped)
        else:
            if not pending.future.done():
                pending.future.set_result(result)

    def _fail_pending_ops(self, reason: str) -> None:
        """Resolve every queued op's Future with WriteOpFailedError."""
        if self._queue is None:
            return
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STOP:
                continue
            assert isinstance(item, _PendingOp)
            if not item.future.done():
                item.future.set_exception(WriteOpFailedError(reason))
