"""Idempotency guard for the intake surface.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The
``(workspace_id, source, idempotency_key)`` composite is the canonical
de-dup key for every TriggerEvent we accept.
"""

from __future__ import annotations

import uuid

import structlog

logger = structlog.get_logger(__name__)


async def is_duplicate(
    *,
    workspace_id: uuid.UUID,
    source: str,
    idempotency_key: str,
) -> bool:
    """Return ``True`` if this trigger was already seen.

    Lookup is by the composite ``(workspace_id, source, idempotency_key)``
    unique index on :class:`backend.intake.db.TriggerEventRow`.
    """
    # TODO(bundle-g-integration): SELECT 1 FROM trigger_events WHERE ...
    # — see backend/intake/db.py uq_trigger_events_ws_src_key.
    logger.debug(
        "idempotency_check_stub",
        workspace_id=str(workspace_id),
        source=source,
        key=idempotency_key,
    )
    raise NotImplementedError("is_duplicate pending Bundle G integration")


async def record(
    *,
    workspace_id: uuid.UUID,
    source: str,
    idempotency_key: str,
    trigger_event_id: uuid.UUID,
) -> None:
    """Persist the idempotency marker. Called inside the intake
    transaction so duplicate rows fail at the DB unique constraint."""
    # TODO(bundle-g-integration): INSERT INTO trigger_events ... ON CONFLICT
    # DO NOTHING and propagate the conflict back to the caller.
    logger.debug(
        "idempotency_record_stub",
        workspace_id=str(workspace_id),
        source=source,
        key=idempotency_key,
        trigger_event_id=str(trigger_event_id),
    )
    raise NotImplementedError("record pending Bundle G integration")


__all__ = ["is_duplicate", "record"]
