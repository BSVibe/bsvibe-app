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

from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow


@runtime_checkable
class WorkspaceScheduleRepository(Protocol):
    """Persistence seam for ``workspace_schedules`` rows.

    The Protocol is intentionally tick-shaped (claim a batch, advance one
    row at a time). The runner owns the transaction; the repository
    never calls ``commit`` and never opens a new transaction. Mutations
    are flushed so the in-memory row carries the post-update state to
    the caller (matches the Workflow / Identity Repository conventions).
    """

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
