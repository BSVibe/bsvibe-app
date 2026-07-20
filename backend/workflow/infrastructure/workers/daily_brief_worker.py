"""DailyBriefWorker — the once-a-day founder digest producer (Notifier daily_brief).

The fifth notification moment. Unlike the four terminal-write producers
(``needs_you`` / ``triggered`` / ``shipped`` / ``failed``), ``daily_brief`` has
no single triggering write — it is a per-workspace DIGEST, so it is a dedicated
poll-loop worker rather than a terminal-write side effect. It is deliberately
NOT modelled as a :class:`~backend.schedule.infrastructure.schedule_db.WorkspaceScheduleRow`
instruction row: those mint a RUN (via ``ScheduleTrigger`` → ``TriggerEvent`` →
``Request``); ``daily_brief`` produces a NOTIFICATION directly, so a schedule
row would be the wrong shape.

Each tick, for every non-deleted workspace, the worker:

1. Skips the workspace unless ``daily_brief`` is enabled for at least one channel
   in its notification-prefs matrix (incl. ``in_app`` — an in-app brief still
   surfaces in the Brief inbox). ``daily_brief`` defaults OFF
   (:data:`~backend.notifications.db.DEFAULT_MATRIX`), so this is strictly
   opt-in: a workspace with no prefs row is skipped.
2. Skips the workspace unless the current instant, evaluated in the workspace's
   own IANA ``timezone``, falls in the ``[morning_hour, morning_hour + 1)``
   local-morning window (default 08:00–09:00). An Asia/Seoul workspace briefs at
   KST morning, a UTC workspace at UTC morning.
3. Gathers a SMALL deterministic (no-LLM) digest: how many runs SHIPPED and
   FAILED in the last 24h, and how many Decisions are currently PENDING (awaiting
   the founder).
4. Emits ONE ``daily_brief`` :class:`~backend.notifications.db.NotificationEventRow`
   through the shared :func:`~backend.notifications.emit.emit_notification` seam,
   keyed ``daily_brief:<workspace_id>:<local_date>``. That local-date dedupe key
   + the outbox UNIQUE constraint make the brief exactly-once-per-local-day: a
   second tick inside the same morning window is a DB-level no-op.

Delivery is the :class:`~backend.workflow.infrastructure.workers.notify_worker.NotifyWorker`'s
job — it drains the row and fans it out per the prefs matrix + quiet hours, the
same as every other notification. This worker only PRODUCES.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import DEFAULT_MATRIX, NotificationPrefsRow
from backend.notifications.emit import emit_notification
from backend.workers.base import BaseWorker
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

logger = structlog.get_logger(__name__)

#: The channel this worker declares itself as when emitting (must be listed in
#: ``NOTIFICATION_OUTBOX.producers`` — the INV-1 producer guard enforces it).
PRODUCER_ID = "worker:daily_brief"

#: Deterministic (no-LLM) founder-facing copy. Matches the sibling producers
#: (needs_you / triggered / shipped / failed), which also use fixed English
#: strings — notifications are not routed through the LLM language directive.
_BRIEF_TITLE = "Your daily brief"
#: The founder lands on the Brief (their digest home; decisions + activity live
#: one tap away).
_BRIEF_LINK = "/brief"

#: The rolling window the shipped/failed counts summarise.
_DIGEST_WINDOW = timedelta(hours=24)


@dataclass(slots=True)
class DailyBriefWorkerConfig:
    """Tunables for the daily-brief poll loop.

    ``morning_hour`` is the local hour the brief fires at; the fire window is the
    single hour ``[morning_hour, morning_hour + 1)`` in the workspace's own tz.
    ``poll_interval_s`` MUST be well under an hour so at least one tick lands
    inside that window (the local-date dedupe key collapses the repeats).
    """

    poll_interval_s: float = 600.0
    morning_hour: int = 8


def _resolve_tz(timezone: str) -> tzinfo:
    """The workspace's IANA zone (falls back to UTC on an unknown name)."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _daily_brief_enabled(matrix: dict[str, dict[str, bool]]) -> bool:
    """Has this workspace opted in on ANY channel (incl. ``in_app``)?

    An in-app-only brief is still worth producing — it surfaces in the Brief
    inbox even with no push channel bound.
    """
    return any(bool(on) for on in matrix.get("daily_brief", {}).values())


def _digest_body(*, shipped: int, failed: int, pending: int) -> str:
    """The short deterministic one-line summary the founder reads."""
    return f"Today: {shipped} shipped · {failed} failed · {pending} decisions awaiting you"


class DailyBriefWorker(BaseWorker):
    """Per-workspace once-a-day digest producer for the ``daily_brief`` event."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: DailyBriefWorkerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfg = config or DailyBriefWorkerConfig()
        super().__init__(name="daily_brief_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        # Injected so the morning-window gate is deterministic under test; the
        # default is the real wall clock in UTC.
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def _tick(self) -> int:
        return await self.run_once()

    async def run_once(self) -> int:
        """One pass over every workspace; return how many briefs were produced.

        The count is the number of workspaces that were both opted in AND inside
        their local morning window this tick (an already-briefed day is a
        dedupe no-op inside :func:`emit_notification`, so a re-tick returns the
        same set but writes nothing new)."""
        now_utc = self._clock()
        produced = 0
        async with self._session_factory() as session:
            for workspace in await self._active_workspaces(session):
                if await self._brief_workspace(session, workspace, now_utc):
                    produced += 1
            await session.commit()
        return produced

    async def _brief_workspace(
        self, session: AsyncSession, workspace: WorkspaceRow, now_utc: datetime
    ) -> bool:
        """Emit one brief for ``workspace`` if it is opted in AND at local morning."""
        matrix = await self._matrix(session, workspace.id)
        if not _daily_brief_enabled(matrix):
            return False

        local_now = now_utc.astimezone(_resolve_tz(workspace.timezone))
        if local_now.hour != self._cfg.morning_hour:
            return False

        shipped, failed, pending = await self._digest(session, workspace.id, now_utc)
        await emit_notification(
            session,
            workspace_id=workspace.id,
            event="daily_brief",
            dedupe_key=f"daily_brief:{workspace.id}:{local_now.date().isoformat()}",
            payload={
                "title": _BRIEF_TITLE,
                "body": _digest_body(shipped=shipped, failed=failed, pending=pending),
                "link": _BRIEF_LINK,
            },
            producer_id=PRODUCER_ID,
        )
        logger.info(
            "daily_brief_emitted",
            workspace_id=str(workspace.id),
            local_date=local_now.date().isoformat(),
            shipped=shipped,
            failed=failed,
            pending=pending,
        )
        return True

    async def _digest(
        self, session: AsyncSession, workspace_id: uuid.UUID, now_utc: datetime
    ) -> tuple[int, int, int]:
        """(shipped-24h, failed-24h, currently-pending-decisions) for a workspace."""
        cutoff = now_utc - _DIGEST_WINDOW
        shipped = await self._count_runs(session, workspace_id, RunStatus.SHIPPED, cutoff)
        failed = await self._count_runs(session, workspace_id, RunStatus.FAILED, cutoff)
        pending = await self._count_pending_decisions(session, workspace_id)
        return shipped, failed, pending

    @staticmethod
    async def _count_runs(
        session: AsyncSession, workspace_id: uuid.UUID, status: RunStatus, cutoff: datetime
    ) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(ExecutionRun)
            .where(
                ExecutionRun.workspace_id == workspace_id,
                ExecutionRun.status == status,
                ExecutionRun.updated_at >= cutoff,
            )
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def _count_pending_decisions(session: AsyncSession, workspace_id: uuid.UUID) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Decision)
            .where(
                Decision.workspace_id == workspace_id,
                Decision.status == DecisionStatus.PENDING,
            )
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def _active_workspaces(session: AsyncSession) -> list[WorkspaceRow]:
        result = await session.execute(
            select(WorkspaceRow).where(WorkspaceRow.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    @staticmethod
    async def _matrix(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, dict[str, bool]]:
        """The workspace's prefs matrix (its own, or the default seed matrix)."""
        prefs = (
            await session.execute(
                select(NotificationPrefsRow).where(
                    NotificationPrefsRow.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        return prefs.matrix if prefs is not None else DEFAULT_MATRIX


__all__ = [
    "DailyBriefWorker",
    "DailyBriefWorkerConfig",
]
