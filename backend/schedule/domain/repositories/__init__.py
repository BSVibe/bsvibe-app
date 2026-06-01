"""Repository Protocols — application-layer seam onto Schedule persistence.

Lift I-Repo-Final Phase B. The :class:`WorkspaceScheduleRepository`
Protocol replaces the direct ``select(WorkspaceScheduleRow)`` /
``with_for_update`` access pattern in
:mod:`backend.schedule.infrastructure.db_poll_runner` with a stable
seam. Concrete impl lives in
:mod:`backend.schedule.infrastructure.repositories`.
"""

from __future__ import annotations

from backend.schedule.domain.repositories.workspace_schedule_repository import (
    WorkspaceScheduleRepository,
)

__all__ = ["WorkspaceScheduleRepository"]
