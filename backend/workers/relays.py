"""Relay sinks for the audit outbox + config-driven relay selection.

The :class:`~backend.workers.relay_worker.RelayWorker` drains
``audit_outbox`` and ships each batch through a
:class:`~backend.workers.relay_worker.Relay`. Two relays exist:

* :class:`~backend.workers.run.LoggingRelay` — the no-sink default (log + ack
  the whole batch so audit rows do not accumulate, but no remote delivery).
* :class:`HttpRelay` — POSTs the batch as JSON to a configured HTTP endpoint.

:func:`build_relay` selects between them by ``settings.audit_relay_url``:
non-empty → :class:`HttpRelay`, empty → :class:`LoggingRelay`.

``HttpRelay`` honors the :class:`~backend.workers.relay_worker.Relay`
``send`` contract precisely — it returns ONLY the ids the sink accepted:

* on a 2xx response → ack the whole batch (every id).
* on a non-2xx response OR a transport/network error → ack NOTHING
  (return ``[]``). The records stay in the outbox and the worker retries
  them next tick — a failed POST never loses audit data.
* the relay never raises: any failure is logged and yields ``[]`` so the
  worker loop keeps running (soft-fail).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from backend.extensions.implementations.audit.models import AuditOutboxRecord

if TYPE_CHECKING:
    from backend.config import Settings
    from backend.workers.relay_worker import Relay

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 10.0


def _serialize(record: AuditOutboxRecord) -> dict[str, Any]:
    """JSON-serializable view of one outbox row (what the sink ingests)."""
    return {
        "id": record.id,
        "event_id": record.event_id,
        "event_type": record.event_type,
        "occurred_at": record.occurred_at.isoformat(),
        "payload": record.payload,
    }


class HttpRelay:
    """A :class:`~backend.workers.relay_worker.Relay` that POSTs the batch to
    ``url`` and acks the whole batch only on a 2xx response.

    On any failure (non-2xx or transport error) it acks NOTHING — the records
    remain in the outbox for the next drain tick (no audit-data loss). It never
    raises, so the worker loop keeps running.
    """

    def __init__(
        self,
        *,
        url: str,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._client = client
        self._timeout_s = timeout_s

    async def send(self, records: Sequence[AuditOutboxRecord]) -> Sequence[int]:
        ids = [r.id for r in records]
        if not ids:
            return []

        body = {"records": [_serialize(r) for r in records]}
        try:
            if self._client is not None:
                resp = await self._client.post(self._url, json=body)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            logger.warning("audit_relay_http_error", count=len(ids), error=str(exc), exc_info=True)
            return []

        if resp.is_success:
            logger.info("audit_relay_delivered", count=len(ids), status=resp.status_code)
            return ids

        logger.warning(
            "audit_relay_rejected",
            count=len(ids),
            status=resp.status_code,
        )
        return []


def build_relay(settings: Settings) -> Relay:
    """Select the audit relay by config.

    ``settings.audit_relay_url`` set → :class:`HttpRelay` (remote HTTP sink).
    Empty → :class:`~backend.workers.run.LoggingRelay` (the explicit no-sink
    default: drain + ack, never deliver). Returned typed as the
    :class:`~backend.workers.relay_worker.Relay` Protocol the worker consumes.
    """
    # Imported here (not at module top) to avoid a circular import:
    # ``backend.workers.run`` imports this module to build the worker set, so a
    # top-level ``from backend.workers.run import ...`` would hit a half-defined
    # ``run`` module during its own import.
    from backend.workers.run import LoggingRelay  # noqa: PLC0415 — circular-import guard

    url = settings.audit_relay_url.strip()
    if url:
        logger.info("audit_relay_selected", relay="http", url=url)
        return HttpRelay(url=url)
    logger.info("audit_relay_selected", relay="logging")
    return LoggingRelay()


__all__ = ["HttpRelay", "build_relay"]
