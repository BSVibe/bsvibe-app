"""Schedule emitter persistence — ``workspace_schedules``.

Workflow §12.5 #8 (Bundle G — Intake / Triggers) carry-over (M1). The
:class:`~backend.schedule.application.emitter.ScheduleTrigger` adapter
turns a *fire time* into a
:class:`~backend.workflow.infrastructure.intake.db.TriggerEventRow`, but
nothing in production told it WHEN to fire — there was no row whose
``next_run_at`` a runner could poll, and no consumer that drove the
schedule end of the OS at all (Status §5).

``WorkspaceScheduleRow`` is the durable schedule the runner polls. Each
row is "plugin X on cron expr Y in workspace W, due next at
``next_run_at``." The runner
(:class:`~backend.schedule.infrastructure.workers.schedule_worker.ScheduleWorker`)
selects ``enabled AND next_run_at <= now`` rows, fires the emitter, and
advances ``next_run_at`` via a swappable
:class:`~backend.schedule.domain.advancer.ScheduleAdvancer` seam — so the
cron algebra itself can evolve (or be replaced) without rewriting the
runner.

We deliberately keep the row minimal: this is the *trigger source* the
runner needs, not a full per-plugin configuration store (the plugin
owns that).

Note: the row registers on the shared ``backend.data.Base.metadata`` so
alembic + the runtime ORM both see it. The corresponding migration stays
at :mod:`backend.data.migrations.versions.20260619_workspace_schedules` —
only the SQLAlchemy model moves with the Schedule lift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base


class WorkspaceScheduleRow(Base):
    """Durable schedule the schedule runner polls.

    A workspace may carry many rows (one per scheduled plugin × cron expr),
    each independently enabled. The ``(workspace_id, plugin_name, cron_expr)``
    unique constraint blocks accidentally registering the same trigger
    twice; a deliberately *different* cron expr is a different row.
    """

    __tablename__ = "workspace_schedules"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "plugin_name",
            "cron_expr",
            name="uq_workspace_schedules_ws_plugin_cron",
        ),
        Index(
            "ix_workspace_schedules_due",
            "enabled",
            "next_run_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    plugin_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(255), nullable=False)
    # The next time the runner should fire this schedule. The runner
    # selects rows with ``enabled=True AND next_run_at <= now``, fires the
    # emitter, then advances ``next_run_at`` via the
    # :class:`~backend.schedule.domain.advancer.ScheduleAdvancer` seam.
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Audit / glass-box marker so an operator can tell the schedule has
    # in fact fired at least once (vs. an enabled but never-due row).
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


__all__ = ["WorkspaceScheduleRow"]
