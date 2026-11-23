"""AdversarialReplayWorker — runs the queued Stage A replays, one at a time.

The MCP tool ``bsvibe_adversarial_replay`` only registers a row in ``adversarial_replay_jobs``;
this worker claims it (:func:`claim_next` — which refuses while another replay is running with a
live heartbeat), runs it and records every case's result on the row. See
:mod:`backend.workflow.application.adversarial_replay_jobs` for the rules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workers.base import BaseWorker
from backend.workflow.application.adversarial_replay_jobs import claim_next
from backend.workflow.infrastructure.db import AdversarialReplayJob

logger = structlog.get_logger(__name__)

#: A job is a human-triggered measurement; a few seconds of pickup latency is irrelevant.
_POLL_INTERVAL_S = 5.0

Execute = Callable[[AdversarialReplayJob], Awaitable[None]]


class AdversarialReplayWorker(BaseWorker):
    """Claim → run → record; one job per tick."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        execute: Execute,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        super().__init__(name="adversarial_replay_worker", poll_interval_s=poll_interval_s)
        self._session_factory = session_factory
        self._execute = execute

    async def _tick(self) -> int:
        return await self.run_once()

    async def run_once(self) -> int:
        """Run at most one queued job to the end; return how many ran (0 or 1)."""
        job = await claim_next(self._session_factory)
        if job is None:
            return 0
        logger.info("adversarial_replay_job_started", job_id=str(job.id))
        await self._execute(job)
        return 1


__all__ = ["AdversarialReplayWorker"]
