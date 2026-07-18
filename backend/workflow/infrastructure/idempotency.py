"""Idempotency guard for the intake surface.

Workflow §12.5 #8 (Bundle G — Intake / Triggers). The
``(workspace_id, source, idempotency_key)`` composite is the canonical
de-dup key for every TriggerEvent we accept.

Persistence is via :class:`backend.workflow.infrastructure.intake.db.TriggerEventRow`
and its unique constraint ``uq_trigger_events_ws_src_key`` — the DB is the
source of truth, this module just exposes the read + write surface.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.channels import TRIGGER_EVENTS
from backend.workflow.infrastructure.intake.db import TriggerEventRow

logger = structlog.get_logger(__name__)


async def is_duplicate(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source: str,
    idempotency_key: str,
) -> bool:
    """Return ``True`` if a row with this triple already exists."""
    stmt = select(TriggerEventRow.id).where(
        TriggerEventRow.workspace_id == workspace_id,
        TriggerEventRow.source == source,
        TriggerEventRow.idempotency_key == idempotency_key,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def record(
    session: AsyncSession,
    *,
    row: TriggerEventRow,
    producer_id: str,
) -> None:
    """Write ``row`` through the ``trigger_events`` channel, then flush.

    The channel guards the add behind ``producer_id`` (INV-1); the flush
    surfaces conflicts as :class:`sqlalchemy.exc.IntegrityError` so callers
    can catch and treat as duplicate. The caller still owns commit/rollback.
    """
    TRIGGER_EVENTS.emit(session, row, producer_id=producer_id)
    await session.flush()
    logger.debug(
        "idempotency_recorded",
        workspace_id=str(row.workspace_id),
        source=row.source,
        idempotency_key=row.idempotency_key,
        trigger_event_id=str(row.id),
    )


__all__ = ["is_duplicate", "record"]
