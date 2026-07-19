"""ScheduleService — the schedule authoring producer (S1).

The application-layer service behind ``POST/GET/DELETE/PATCH /api/v1/schedules``.
It is the missing PRODUCER of the ``workspace_schedules`` channel: before S1 the
``ScheduleWorker`` polled that table every 10s in prod but NOTHING wrote rows to
it, so it was a dead channel — BSVibe could not start work on its own.

``create`` validates the cron expression at authoring time (an invalid expr is a
400, never a silently-never-firing row), computes the first ``next_run_at`` via
the :class:`~backend.schedule.domain.advancer.CronScheduleAdvancer` (so the row
is immediately pollable), and inserts through the INV-1
:data:`~backend.schedule.channels.WORKSPACE_SCHEDULES` producer seam.

S1 supports the ``instruction`` kind only — ``payload={"text": <what to do>}``.
The run framer reads ``text``, so a scheduled run frames the founder's
instruction rather than "Untitled run". skill / product_tick / plugin_action
kinds are S4.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.schedule.domain.advancer import CronScheduleAdvancer
from backend.schedule.domain.cron import CronParseError, parse_cron
from backend.schedule.infrastructure.repositories.workspace_schedule_repository_sql import (
    SqlAlchemyWorkspaceScheduleRepository,
)
from backend.schedule.infrastructure.schedule_db import (
    SCHEDULE_KIND_INSTRUCTION,
    WorkspaceScheduleRow,
)

_PRODUCER_ID = "api:schedules_create"


class ScheduleValidationError(ValueError):
    """The requested schedule is invalid (bad cron expr / unsupported kind)."""


class ScheduleService:
    """Create + manage ``workspace_schedules`` rows for the REST surface."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = SqlAlchemyWorkspaceScheduleRepository(session)
        self._advancer = CronScheduleAdvancer()

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        text: str,
        cron_expr: str,
        product_id: uuid.UUID | None = None,
        title: str | None = None,
        now: datetime | None = None,
    ) -> WorkspaceScheduleRow:
        """Author one schedule. Raises :class:`ScheduleValidationError` on a bad
        kind or cron expression (the REST layer maps that to a 400)."""
        if kind != SCHEDULE_KIND_INSTRUCTION:
            raise ScheduleValidationError(
                f"unsupported schedule kind {kind!r} (S1 supports only "
                f"{SCHEDULE_KIND_INSTRUCTION!r})"
            )
        if not text.strip():
            raise ScheduleValidationError("instruction text must not be empty")
        try:
            parse_cron(cron_expr)
        except CronParseError as exc:
            raise ScheduleValidationError(f"invalid cron expression: {exc}") from exc

        after = now or datetime.now(tz=UTC)
        next_run_at = self._advancer.next_after(cron_expr=cron_expr, after=after)
        payload: dict[str, Any] = {"text": text}
        row = WorkspaceScheduleRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            kind=kind,
            payload=payload,
            title=title,
            plugin_name=None,
            cron_expr=cron_expr,
            next_run_at=next_run_at,
            last_fired_at=None,
            enabled=True,
        )
        await self._repo.create(row, producer_id=_PRODUCER_ID)
        return row

    async def list(self, *, workspace_id: uuid.UUID) -> list[WorkspaceScheduleRow]:
        return await self._repo.list_for_workspace(workspace_id=workspace_id)

    async def get(
        self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> WorkspaceScheduleRow | None:
        return await self._repo.get(schedule_id=schedule_id, workspace_id=workspace_id)

    async def delete(self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        return await self._repo.delete(schedule_id=schedule_id, workspace_id=workspace_id)

    async def set_enabled(
        self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID, enabled: bool
    ) -> WorkspaceScheduleRow | None:
        return await self._repo.set_enabled(
            schedule_id=schedule_id, workspace_id=workspace_id, enabled=enabled
        )


__all__ = ["ScheduleService", "ScheduleValidationError"]
