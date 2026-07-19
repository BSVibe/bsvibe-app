"""WorkspaceScheduleRepository Protocol — claim-due + advance seam.

Lift I-Repo-Final Phase B. v8 §22 #11 + D44/D45. Today the DB-poll runner
(:class:`~backend.schedule.infrastructure.db_poll_runner.DbPollScheduleRunner`)
issues a raw
``select(WorkspaceScheduleRow).where(enabled.is_(True), next_run_at <= now).with_for_update(skip_locked=True)``
plus per-row mutations to advance ``next_run_at`` + ``last_fired_at``.
This Protocol moves the read + the mutations behind a stable seam:

* :meth:`claim_due` — return every due, enabled row for this tick
  (``SELECT … FOR UPDATE SKIP LOCKED`` on PG; the no-op equivalent on
  SQLite). The Protocol does NOT promise a particular dialect's locking
  semantics — the runner depends on the **batch** being claimable, not
  on what claim primitive the impl uses. A future
  ``RedisStreamScheduleRepository`` can satisfy this Protocol without
  touching the runner.

* :meth:`advance` — flip a single row's ``next_run_at`` (and stamp
  ``last_fired_at``). Per-row mutation kept narrow so the runner can't
  accidentally mutate any other column.

Method surface limited to what the v1 ``DbPollScheduleRunner`` actually
needs today — new methods get added per real caller, never speculatively.

Concrete impl:
:mod:`backend.schedule.infrastructure.repositories.workspace_schedule_repository_sql`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow


@runtime_checkable
class WorkspaceScheduleRepository(Protocol):
    """Persistence seam for ``workspace_schedules`` rows.

    Two method families: the tick-shaped consumer side (``claim_due`` /
    ``advance``, used by the runner) and the authoring side (``create`` /
    ``list_for_workspace`` / ``get`` / ``delete`` / ``set_enabled``, used by the
    REST service). The caller owns the transaction; the repository never calls
    ``commit`` and never opens a new transaction. Mutations are flushed so the
    in-memory row carries the post-update state to the caller (matches the
    Workflow / Identity Repository conventions).

    ``create`` routes the insert through the INV-1
    :data:`~backend.schedule.channels.WORKSPACE_SCHEDULES` channel, NOT a bare
    ``session.add`` — the ``emit`` seam is the only legal producer path.
    """

    async def create(self, row: WorkspaceScheduleRow, *, producer_id: str) -> None:
        """Stage one authored schedule row through the channel producer seam."""

    async def list_for_workspace(self, *, workspace_id: uuid.UUID) -> list[WorkspaceScheduleRow]:
        """List every schedule for a workspace, newest first."""

    async def get(
        self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> WorkspaceScheduleRow | None:
        """Fetch one workspace-scoped schedule by id (None if absent/foreign)."""

    async def delete(self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        """Delete one workspace-scoped schedule. Returns whether a row matched."""

    async def set_enabled(
        self, *, schedule_id: uuid.UUID, workspace_id: uuid.UUID, enabled: bool
    ) -> WorkspaceScheduleRow | None:
        """Flip ``enabled`` on one workspace-scoped row (None if absent/foreign)."""

    async def claim_due(self, *, now: datetime) -> list[WorkspaceScheduleRow]:
        """Return every due, enabled schedule for this polling tick.

        Implementations SHOULD apply the dialect's strongest available
        non-blocking lock (PG: ``SELECT … FOR UPDATE SKIP LOCKED``;
        SQLite: best-effort, no-op). Ordering: oldest ``next_run_at``
        first — the runner relies on that order to drain steady-state.
        """

    async def advance(
        self,
        row: WorkspaceScheduleRow,
        *,
        next_run_at: datetime,
        last_fired_at: datetime,
    ) -> WorkspaceScheduleRow:
        """Flip one row's ``next_run_at`` + ``last_fired_at``.

        Returns the same row (mutated in place) for ergonomics. Flushes
        so the row's new state is visible to a subsequent ``claim_due``
        in the same transaction (cf. the v1 runner's per-tick window).
        """


__all__ = ["WorkspaceScheduleRepository"]
