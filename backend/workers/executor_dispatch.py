"""ExecutorDispatchWorker — assign pending executor tasks to available workers.

Workflow §12.5 #8 (Bundle G — Workers). Lift 2 of the executor-pool epic: the
*dispatch consumer*. It scans ``executor_tasks`` for a ``pending`` row, picks an
available worker (:func:`backend.executors.dispatch.find_available_worker`) and
dispatches it (:func:`~backend.executors.dispatch.dispatch_task` — XADD onto the
worker's stream + flip the row to ``dispatched``).

Scope (deliberately minimal):

* **No worker available → leave the task pending.** The tick returns 0 and the
  row is untouched, so it is retried on the next pass. Never crashes.
* **No run-path integration.** A run becoming an ``executor_task`` is Lift 5;
  this worker only moves an already-created ``pending`` task onto a worker
  stream. No real CLI / subprocess (Lift 3) lives here.

Follows the :class:`backend.workers.base.BaseWorker` single-tick pattern: the
public :meth:`dispatch_once` is what tests call; :meth:`_tick` delegates to it
so ``start`` / ``stop`` reuse the shared stop-event-guarded loop.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.executors import dispatch
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)

# DB-polling cadence between dispatch passes (matches the sibling workers).
_DEFAULT_POLL_INTERVAL_S = 5.0


class ExecutorDispatchWorker(BaseWorker):
    """Periodic dispatch of ``pending`` executor tasks to available workers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Any,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        super().__init__(name="executor_dispatch_worker", poll_interval_s=poll_interval_s)
        self._session_factory = session_factory
        self._redis = redis

    async def _tick(self) -> int:
        return await self.dispatch_once()

    async def dispatch_once(self) -> int:
        """Dispatch the oldest pending task to an available worker.

        Returns 1 when a task was dispatched, 0 when there was nothing to do OR
        no worker was available (the task stays ``pending`` for a later pass).
        Soft on the no-worker case — never raises.
        """
        async with self._session_factory() as session:
            task = await dispatch.claim_pending_task(session)
            if task is None:
                return 0

            worker = await dispatch.find_available_worker(
                session,
                workspace_id=task.workspace_id,
                executor_type=task.executor_type,
            )
            if worker is None:
                # No capable, online worker right now — leave the task pending.
                logger.debug(
                    "executor_dispatch_no_worker",
                    task_id=str(task.id),
                    workspace_id=str(task.workspace_id),
                    executor_type=task.executor_type,
                )
                return 0

            await dispatch.dispatch_task(
                self._redis, session=session, task=task, worker_id=worker.id
            )
            await session.commit()
            logger.info(
                "executor_dispatch_assigned",
                task_id=str(task.id),
                worker_id=str(worker.id),
            )
            return 1


__all__ = ["ExecutorDispatchWorker"]
