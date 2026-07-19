"""Schedule-context channel declaration (INV-1).

Declares the ``workspace_schedules`` channel next to its row
(:class:`~backend.schedule.infrastructure.schedule_db.WorkspaceScheduleRow`).
Imports only the schedule-context row + the generic
:class:`~backend.channels.Channel` core — no cross-context imports.

``workspace_schedules`` is the FIRST ``human_origin=True`` channel: unlike
``trigger_events`` / ``requests`` / ``notification_outbox`` (all machine-minted
side effects), a schedule row is AUTHORED by the founder — it is BSVibe's own
"start work on your own" input. The producers are the two authoring surfaces
that share one canonical :class:`~backend.schedule.application.schedule_service.ScheduleService`
path: the REST surface (``api:schedules_create`` → ``POST /api/v1/schedules``)
and its MCP parity tools (``mcp:schedules_create`` → ``bsvibe_schedules_create``,
S2). The sole worker-claim consumer is the
:class:`~backend.schedule.infrastructure.workers.schedule_worker.ScheduleWorker`
(``worker:schedule_worker``), which DB-polls ``enabled AND next_run_at <= now``
rows and fires each through the emitter.

Because ``human_origin=True``, the INV-1 completeness meta-test additionally
requires a non-empty ``authoring_surface`` — a schedule row that no surface can
create would be a dead channel (the exact defect this slice closes). Registering
the channel in :mod:`backend.channels.registry` arms the INV-1 producer guard,
which forbids a bare ``session.add(WorkspaceScheduleRow(...))`` anywhere in
production code — ``WORKSPACE_SCHEDULES.emit`` becomes the only legal write, so
the producer can never be silently forgotten or bypassed.
"""

from __future__ import annotations

from backend.channels import Channel
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow

WORKSPACE_SCHEDULES: Channel[WorkspaceScheduleRow] = Channel(
    name="workspace_schedules",
    row=WorkspaceScheduleRow,
    producers=("api:schedules_create", "mcp:schedules_create"),
    consumers=("worker:schedule_worker",),
    human_origin=True,
    authoring_surface="POST /api/v1/schedules",
)

__all__ = ["WORKSPACE_SCHEDULES"]
