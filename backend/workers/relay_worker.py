"""RelayWorker — relay audit events to the supervisor.

Workflow §12.5 #8 (Bundle G — Workers). Drains the local
``audit_relay`` outbox into the supervisor's ingest API, tracking
the high-water mark in :class:`backend.workers.db.AuditRelayStateRow`.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class RelayWorker:
    """Audit-relay drain to supervisor."""

    async def start(self) -> None:
        """Cursor-based drain of audit_relay outbox → supervisor."""
        # TODO(bundle-g-integration): lift from BSGateway
        # bsgateway/audit_relay/worker.py — POSTs to supervisor with
        # retry + backoff, updates AuditRelayStateRow.cursor.
        logger.debug("relay_worker_start_stub")
        raise NotImplementedError("RelayWorker.start pending Bundle G integration")

    async def stop(self) -> None:
        """Graceful drain."""
        # TODO(bundle-g-integration): cancel + close.
        logger.debug("relay_worker_stop_stub")
        raise NotImplementedError("RelayWorker.stop pending Bundle G integration")


__all__ = ["RelayWorker"]
