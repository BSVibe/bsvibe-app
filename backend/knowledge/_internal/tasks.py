"""Orphan-safe ``asyncio.create_task`` wrappers.

Plain ``asyncio.create_task(coro())`` has two well-known traps:

1. The returned task is the only strong reference. If the caller doesn't keep
   it, the event loop may garbage-collect the task mid-flight (PEP 3156 / CPython
   issue gh-91887).
2. Exceptions raised inside the coroutine are swallowed unless something later
   awaits the task or registers a ``Task.add_done_callback``. By the time the
   asyncio "Task exception was never retrieved" warning fires, context (which
   call site spawned it, what state was at play) is gone.

``spawn_task`` patches both holes:

* keeps a strong reference in a module-level ``set`` until the task finishes;
* attaches a done-callback that logs unhandled exceptions through structlog
  with the supplied ``name`` so failures show up in normal observability.

This implements the BSage half of Audit §5 / Sprint 1 / H16.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Holds strong refs for in-flight tasks so they can't be garbage-collected.
_LIVE_TASKS: set[asyncio.Task[Any]] = set()


def spawn_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` as a background task with safe defaults.

    Args:
        coro: The coroutine to run.
        name: Human-readable identifier — surfaces in logs and asyncio
            introspection. Required (no orphan should be unnamed).

    Returns:
        The asyncio.Task. Callers may await or cancel it; the helper
        manages reference lifetime independently.
    """
    task = asyncio.create_task(coro, name=name)
    _LIVE_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """Done callback: drop the strong ref + surface unhandled exceptions."""
    _LIVE_TASKS.discard(task)
    if task.cancelled():
        # Cancellation is the normal shutdown path — log at debug level only.
        logger.debug("task_cancelled", task=task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "task_failed",
            task=task.get_name(),
            error=type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
