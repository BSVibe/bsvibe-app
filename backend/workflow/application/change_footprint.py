"""Footprints of stored deliverables — settings-aware wrapper over the domain rule."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.domain.change_footprint import ChangeFootprint, footprint_of
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.serialization.deliverable_views import artifact_refs_of


def footprint_of_payload(payload: object) -> ChangeFootprint:
    refs = artifact_refs_of(payload) if isinstance(payload, dict) else []
    return footprint_of(refs, threshold=get_settings().large_change_file_threshold)


async def footprints_for(
    session: AsyncSession, *, workspace_id: uuid.UUID, deliverable_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, ChangeFootprint]:
    """Footprint per deliverable id (workspace-scoped); a missing row is simply absent."""
    ids = set(deliverable_ids)
    if not ids:
        return {}
    stmt = select(Deliverable.id, Deliverable.payload).where(
        Deliverable.id.in_(ids), Deliverable.workspace_id == workspace_id
    )
    rows = (await session.execute(stmt)).all()
    return {row_id: footprint_of_payload(payload) for row_id, payload in rows}


__all__ = ["footprint_of_payload", "footprints_for"]
