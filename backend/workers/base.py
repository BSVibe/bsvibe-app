"""BaseWorker — the polling-loop shell shared by every DB-polling worker.

AgentWorker / DeliveryWorker / VerifierWorker / RelayWorker all had the
identical ``start`` / ``stop`` / ``_run`` machinery (stop-event guarded
background task + "process one batch, then wait poll_interval" loop) and
differed only in the per-batch body. That body stays in each subclass as
``_tick`` (Template Method); the shell lives here once.

The public ``claim_once`` / ``drain_once`` / ``verify_once`` methods that
tests call directly are preserved on the subclasses — each just delegates
``_tick`` to its own batch method.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import structlog

logger = structlog.get_logger(__name__)


class BaseWorker(ABC):
    """Stop-event-guarded background polling loop.

    Subclasses implement :meth:`_tick` (one batch of work) and pass a
    ``name`` (used for the task name + structured-log event prefix) and
    the ``poll_interval_s`` between idle ticks.
    """

    def __init__(self, *, name: str, poll_interval_s: float) -> None:
        self._name = name
        self._poll_interval_s = poll_interval_s
        self._stop_evt = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the background loop (idempotent — no-op if already running)."""
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name=self._name)
        logger.info("worker_started", worker=self._name)

    async def stop(self) -> None:
        """Signal the loop to stop and await its exit."""
        self._stop_evt.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("worker_stopped", worker=self._name)

    @abstractmethod
    async def _tick(self) -> int:
        """Process one batch; return the number of items handled."""

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception("worker_iteration_failed", worker=self._name)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                continue


__all__ = ["BaseWorker"]
