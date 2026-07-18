"""RelayWorker — drain ``audit_outbox`` into a remote sink.

Workflow §12.5 #8 (Bundle G — Workers). Uses
:class:`plugin.audit.OutboxStore` for read + ack, and a caller-
supplied :class:`Relay` adapter to ship each batch. The adapter is a
Protocol so this module stays transport-agnostic — HTTP, gRPC, or in-memory
test sink all satisfy it.

Closes the long-deferred Subscriber_Durability_Followup #1 site: audit emit
no longer fires-and-forgets — :func:`plugin.audit.safe_emit`
lands a row in ``audit_outbox`` inside the request transaction, this worker
drains it on its own schedule, and persistent failures dead-letter via
:meth:`OutboxStore.record_failure`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workers.base import BaseWorker
from plugin.audit.channels import AUDIT_OUTBOX
from plugin.audit.models import AuditOutboxRecord
from plugin.audit.store import OutboxStore

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


class RelayWorker(BaseWorker):
    """Periodic outbox-drain loop."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        relay: Relay,
        store: OutboxStore | None = None,
        config: RelayConfig | None = None,
    ) -> None:
        self._cfg = config or RelayConfig()
        super().__init__(name="relay_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._relay = relay
        self._store = store or OutboxStore()

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Drain one batch from the outbox; return rows delivered.

        Useful for tests + the periodic loop's body.
        """
        async with self._session_factory() as session:
            rows = await AUDIT_OUTBOX.consume(
                consumer_id="worker:relay_worker",
                claim=lambda: self._store.select_undelivered(
                    session, batch_size=self._cfg.batch_size
                ),
            )
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


__all__ = ["Relay", "RelayConfig", "RelayWorker"]
