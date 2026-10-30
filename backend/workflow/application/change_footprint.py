"""Footprints of stored deliverables — settings-aware wrapper over the domain rule."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.domain.change_footprint import (
    ChangeFootprint,
    TopDir,
    footprint_from_counts,
    footprint_of,
)
from backend.workflow.infrastructure.db import Deliverable
from backend.workflow.serialization.deliverable_views import artifact_refs_of


def _recorded_worktree_change(payload: dict[str, Any]) -> tuple[int, list[TopDir]] | None:
    """The verify-time worktree count, or ``None`` when absent / malformed."""
    recorded = payload.get("worktree_change")
    if not isinstance(recorded, dict):
        return None
    total = recorded.get("changed_files")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        return None
    try:
        top_dirs = [TopDir(**d) for d in recorded.get("top_dirs") or []]
    except (TypeError, ValueError):
        top_dirs = []
    return total, top_dirs


def footprint_of_payload(payload: object) -> ChangeFootprint:
    """Worktree count (what delivery commits) first; ``artifact_refs`` only as a fallback."""
    threshold = get_settings().large_change_file_threshold
    if not isinstance(payload, dict):
        return ChangeFootprint()
    recorded = _recorded_worktree_change(payload)
    if recorded is not None:
        return footprint_from_counts(recorded[0], recorded[1], threshold=threshold)
    return footprint_of(artifact_refs_of(payload), threshold=threshold)


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
