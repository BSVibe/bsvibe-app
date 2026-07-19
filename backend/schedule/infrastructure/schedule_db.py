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
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

# The default (and, in S1, only) schedule kind: a natural-language
# ``instruction`` whose ``payload["text"]`` IS the run task. Other kinds
# (skill / product_tick / plugin_action) are deferred to S4.
SCHEDULE_KIND_INSTRUCTION = "instruction"


class WorkspaceScheduleRow(Base):
    """Durable schedule the schedule runner polls.

    A workspace may carry many rows, each independently enabled. In S1 a row
    is a natural-language ``instruction`` (``kind='instruction'``) whose
    ``payload["text"]`` is the task the scheduled run frames + executes. The
    surrogate ``id`` is the sole identity — there is no
    ``(workspace_id, plugin_name, cron_expr)`` uniqueness (two NL rows may
    legitimately share a cron expr, and ``plugin_name`` is NULL for the
    ``instruction`` kind).
    """

    __tablename__ = "workspace_schedules"
    __table_args__ = (
        Index(
            "ix_workspace_schedules_due",
            "enabled",
            "next_run_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # What the schedule fires. ``instruction`` (S1) reads ``payload["text"]``;
    # skill / product_tick / plugin_action kinds arrive in S4.
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SCHEDULE_KIND_INSTRUCTION,
        server_default=SCHEDULE_KIND_INSTRUCTION,
    )
    # The instruction envelope the emitter merges into the TriggerEvent
    # payload. For ``instruction`` this holds ``{"text": "<what to do>"}``;
    # the run framer reads ``text`` so a scheduled run frames the founder's
    # instruction (not "Untitled run").
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    # Short human label for the list surface. Optional — the instruction text
    # is the fallback title.
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # NULL for the ``instruction`` kind (there is no plugin). A future
    # plugin_action kind (S4) populates it.
    plugin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


__all__ = ["SCHEDULE_KIND_INSTRUCTION", "WorkspaceScheduleRow"]
