"""Repository + claim statement for the ``github_merge_watch`` table.

Mirrors the delivery-worker seam: a standalone, unit-testable
:func:`build_merge_watch_claim_stmt` (so a test can pin that the rendered SQL
carries ``FOR UPDATE SKIP LOCKED`` — the load-bearing multi-server guard) plus
a thin :class:`GithubMergeWatchRepository` (``add`` / ``claim_due`` /
``mark_status``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.github.db import (
    GithubMergeWatchRow,
    MergeWatchStatus,
)

# The statuses a poll pass may claim: a PR still waiting on CI, or one flagged
# for conflict resolution (re-claimed so the poller can re-dispatch the run).
_CLAIMABLE_STATUSES = (MergeWatchStatus.PENDING_CI, MergeWatchStatus.NEEDS_RESOLUTION)


def build_merge_watch_claim_stmt(
    *, now: datetime, batch_size: int
) -> Select[tuple[GithubMergeWatchRow]]:
    """Multi-server safe claim of due merge-watch rows.

    ``FOR UPDATE SKIP LOCKED`` makes the SELECT atomic w.r.t. a second poller on
    the same DB: one worker's transaction locks its claimed rows, the other's
    SELECT skips them and picks the rest — exactly one poll pass per row, no
    double-dispatch. Extracted as a builder so a unit test can assert the
    rendered SQL carries the lock hint (the prod guard).
    """
    return (
        select(GithubMergeWatchRow)
        .where(
            GithubMergeWatchRow.status.in_(_CLAIMABLE_STATUSES),
            GithubMergeWatchRow.next_poll_at <= now,
        )
        .order_by(GithubMergeWatchRow.next_poll_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


class GithubMergeWatchRepository:
    """SQLAlchemy-backed persistence for ``github_merge_watch``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, row: GithubMergeWatchRow) -> None:
        """Enqueue one PR under auto-merge watch."""
        self._session.add(row)
        await self._session.flush()

    async def claim_due(self, *, now: datetime, batch_size: int) -> list[GithubMergeWatchRow]:
        """Claim up to ``batch_size`` due, claimable rows (``FOR UPDATE SKIP LOCKED``)."""
        stmt = build_merge_watch_claim_stmt(now=now, batch_size=batch_size)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_status(
        self,
        row_id: uuid.UUID,
        status: MergeWatchStatus,
        *,
        next_poll_at: datetime | None = None,
        last_error: str | None = None,
        increment_attempt: bool = False,
        conflict_dispatched: bool | None = None,
    ) -> None:
        """Transition one row's status (+ optional backoff / attempt / flags)."""
        values: dict[str, Any] = {"status": status}
        if next_poll_at is not None:
            values["next_poll_at"] = next_poll_at
        if last_error is not None:
            values["last_error"] = last_error
        if conflict_dispatched is not None:
            values["conflict_dispatched"] = conflict_dispatched
        if increment_attempt:
            values["attempts"] = GithubMergeWatchRow.attempts + 1
        stmt = update(GithubMergeWatchRow).where(GithubMergeWatchRow.id == row_id).values(**values)
        await self._session.execute(stmt)
        await self._session.flush()


__all__ = [
    "GithubMergeWatchRepository",
    "build_merge_watch_claim_stmt",
]
