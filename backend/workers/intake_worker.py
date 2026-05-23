"""IntakeWorker — drain TriggerEvents into Requests.

Workflow §12.5 #8 (Bundle G — Workers). DB-polling implementation (not
Redis Streams) — pulls :class:`TriggerEventRow` rows that have no paired
:class:`RequestRow` yet, claims them via row-update, and mints the
matching ``Request`` (status ``OPEN``) so the :class:`AgentWorker` can
pick it up.

The Redis Streams variant (consumer-group + XACK) remains a TODO — for
Phase 1 the DB-polling path is simpler to reason about and
integration-test, mirroring :mod:`backend.workers.agent_worker`. A
TriggerEvent is "drained" exactly once because the unprocessed query is
``NOT EXISTS (request with this trigger_event_id)``; once the Request is
committed the event no longer matches.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.intake.db import RequestRow, RequestStatus, TriggerEventRow
from backend.workers.base import BaseWorker
from backend.workers.emit import STREAM_AGENT, emit_stream_notification

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class IntakeWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0


class IntakeWorker(BaseWorker):
    """DB-polling worker that turns un-drained TriggerEvents into Requests.

    Doubles as the *producer* for the ``agent`` stream: each minted Request is
    a row the :class:`~backend.workers.agent_worker.AgentWorker` would poll, so
    when ``worker_mode="redis_streams"`` the worker ALSO emits a notification
    (best-effort, soft-fail) to wake the agent consumer immediately. The DB row
    stays the source of truth — emission only happens AFTER the commit, and a
    Redis hiccup never breaks the drain (DB-polling remains the safety net).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: IntakeWorkerConfig | None = None,
        redis_client: object | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._cfg = config or IntakeWorkerConfig()
        super().__init__(name="intake_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        # Optional — only set in redis_streams mode. None keeps the existing
        # DB-polling behaviour (no emission), so every existing caller/test is
        # unaffected.
        self._redis_client = redis_client
        self._settings = settings or get_settings()

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Drain one batch of un-drained TriggerEvents into Requests. Returns count."""
        count = 0
        emitted_workspace_ids: list[str] = []
        async with self._session_factory() as session:
            async for trig in self._claim_batch(session):
                now = datetime.now(tz=UTC)
                session.add(
                    RequestRow(
                        id=uuid.uuid4(),
                        workspace_id=trig.workspace_id,
                        trigger_event_id=trig.id,
                        status=RequestStatus.OPEN,
                        payload=dict(trig.payload or {}),
                        created_at=now,
                        updated_at=now,
                    )
                )
                logger.info(
                    "intake_worker_request_created",
                    trigger_event_id=str(trig.id),
                    workspace_id=str(trig.workspace_id),
                    source=trig.source,
                )
                emitted_workspace_ids.append(str(trig.workspace_id))
                count += 1
            await session.commit()
        # AFTER the commit (the row is durable) emit a wake-up per new Request.
        # Gated + soft-fail inside the helper — a no-op in DB-polling mode.
        for ws_id in emitted_workspace_ids:
            await emit_stream_notification(
                self._redis_client,  # type: ignore[arg-type]  # narrowed in helper
                settings=self._settings,
                stream=STREAM_AGENT,
                fields={"workspace_id": ws_id},
            )
        return count

    async def _claim_batch(self, session: AsyncSession) -> AsyncIterator[TriggerEventRow]:
        """Yield up to ``batch_size`` TriggerEvents that have no Request yet."""
        already_drained = exists().where(RequestRow.trigger_event_id == TriggerEventRow.id)
        stmt = (
            select(TriggerEventRow)
            .where(~already_drained)
            .order_by(TriggerEventRow.received_at.asc())
            .limit(self._cfg.batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = (await session.execute(stmt)).scalars().all()
        for r in rows:
            yield r


__all__ = ["IntakeWorker", "IntakeWorkerConfig"]
