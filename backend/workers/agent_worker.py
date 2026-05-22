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

from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.agent_runner import AgentRunner
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class AgentWorkerConfig:
    batch_size: int = 10
    poll_interval_s: float = 5.0


class AgentWorker(BaseWorker):
    """DB-polling worker that drives Requests through their first transition."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AgentWorkerConfig | None = None,
    ) -> None:
        self._cfg = config or AgentWorkerConfig()
        super().__init__(name="agent_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory

    async def _tick(self) -> int:
        return await self.claim_once()

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


__all__ = ["AgentWorker", "AgentWorkerConfig"]
