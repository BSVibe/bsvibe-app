"""RelayWorker — drain ``audit_outbox`` into a remote sink.

Workflow §12.5 #8 (Bundle G — Workers). Uses
:class:`backend.supervisor.audit.OutboxStore` for read + ack, and a caller-
supplied :class:`Relay` adapter to ship each batch. The adapter is a
Protocol so this module stays transport-agnostic — HTTP, gRPC, or in-memory
test sink all satisfy it.

Closes the long-deferred Subscriber_Durability_Followup #1 site: audit emit
no longer fires-and-forgets — :func:`backend.supervisor.audit.safe_emit`
lands a row in ``audit_outbox`` inside the request transaction, this worker
drains it on its own schedule, and persistent failures dead-letter via
:meth:`OutboxStore.record_failure`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.supervisor.audit.models import AuditOutboxRecord
from backend.supervisor.audit.store import OutboxStore

logger = structlog.get_logger(__name__)


class Relay(Protocol):
    """Send a batch of audit records to the remote sink.

    Returns the list of record ids that were successfully delivered. Any id
    NOT in the returned list is re-tried (or eventually dead-lettered via
    :meth:`OutboxStore.record_failure`).
    """

    async def send(self, records: Sequence[AuditOutboxRecord]) -> Sequence[int]: ...


@dataclass(slots=True)
class RelayConfig:
    batch_size: int = 100
    poll_interval_s: float = 5.0
    max_retries: int = 5


class RelayWorker:
    """Periodic outbox-drain loop."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        relay: Relay,
        store: OutboxStore | None = None,
        config: RelayConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._relay = relay
        self._store = store or OutboxStore()
        self._cfg = config or RelayConfig()
        self._stop_evt = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the background drain loop."""
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run(), name="relay_worker")
        logger.info("relay_worker_started", batch_size=self._cfg.batch_size)

    async def stop(self) -> None:
        """Signal the loop to stop and await its exit."""
        self._stop_evt.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("relay_worker_stopped")

    async def drain_once(self) -> int:
        """Drain one batch from the outbox; return rows delivered.

        Useful for tests + the periodic loop's body.
        """
        async with self._session_factory() as session:
            rows = await self._store.select_undelivered(session, batch_size=self._cfg.batch_size)
            if not rows:
                return 0

            try:
                delivered = list(await self._relay.send(rows))
            except Exception as exc:  # noqa: BLE001 — record per-row, don't abort
                logger.warning("relay_send_failed", count=len(rows), error=str(exc), exc_info=True)
                for r in rows:
                    await self._store.record_failure(
                        session,
                        r.id,
                        error=str(exc),
                        max_retries=self._cfg.max_retries,
                    )
                await session.commit()
                return 0

            if delivered:
                await self._store.mark_delivered(session, delivered)
            failed_ids = [r.id for r in rows if r.id not in set(delivered)]
            for fid in failed_ids:
                await self._store.record_failure(
                    session,
                    fid,
                    error="upstream rejected",
                    max_retries=self._cfg.max_retries,
                )
            await session.commit()
            return len(delivered)

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.drain_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                logger.exception("relay_worker_iteration_failed")
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._cfg.poll_interval_s)
            except TimeoutError:
                continue


__all__ = ["Relay", "RelayConfig", "RelayWorker"]
