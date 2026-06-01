"""Concrete Repository implementations — :mod:`backend.schedule` (Lift I-Repo-Final).

One :class:`SqlAlchemy<Entity>Repository` per domain Protocol. Constructor
takes one :class:`AsyncSession`; the session owns the transaction (v8 D45).
"""

from __future__ import annotations

from backend.schedule.infrastructure.repositories.workspace_schedule_repository_sql import (
    SqlAlchemyWorkspaceScheduleRepository,
)

__all__ = ["SqlAlchemyWorkspaceScheduleRepository"]
