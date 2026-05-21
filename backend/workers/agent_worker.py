"""AgentWorker — consume pending Requests and advance them via AgentRunner.

Workflow §12.5 #8 (Bundle G — Workers). DB-polling implementation (not
Redis Streams) — pulls ``status=OPEN`` Requests from the ``requests``
table, claims them via row-update, and hands each to
:class:`backend.orchestrator.AgentRunner` to mint an ExecutionRun.

The Redis Streams variant (with proper consumer-group semantics + XACK)
remains a TODO — for Phase 1 the DB-polling path is simpler to reason
about and integration-test, and the load is bounded by Request volume.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.agent_runner import AgentRunner

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class AgentWorkerConfig:
    batch_size: int = 10
    poll_interval_s: float = 5.0


class AgentWorker:
    """DB-polling worker that drives Requests through their first transition."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AgentWorkerConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cfg = config or AgentWorkerConfig()
        self._stop_evt = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Spin up the polling loop."""
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="agent_worker")
        logger.info("agent_worker_started", batch_size=self._cfg.batch_size)

    async def stop(self) -> None:
        """Graceful drain."""
        self._stop_evt.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("agent_worker_stopped")

    async def claim_once(self) -> int:
        """Pull one batch of OPEN Requests, advance to RUNNING. Returns count."""
        count = 0
        async with self._session_factory() as session:
            async for req in self._claim_batch(session):
                runner = AgentRunner(session)
                run_id = await runner.open_run(request=req)
                req.status = RequestStatus.RUNNING
                await session.flush()
                logger.info(
                    "agent_worker_claimed",
                    request_id=str(req.id),
                    run_id=str(run_id),
                )
                count += 1
            await session.commit()
        return count

    async def _claim_batch(self, session: AsyncSession) -> AsyncIterator[RequestRow]:
        """Yield up to ``batch_size`` OPEN requests within ``session``."""
        stmt = (
            select(RequestRow)
            .where(RequestRow.status == RequestStatus.OPEN)
            .order_by(RequestRow.created_at.asc())
            .limit(self._cfg.batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = (await session.execute(stmt)).scalars().all()
        for r in rows:
            yield r

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.claim_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception("agent_worker_iteration_failed")
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._cfg.poll_interval_s)
            except TimeoutError:
                continue


__all__ = ["AgentWorker", "AgentWorkerConfig"]
