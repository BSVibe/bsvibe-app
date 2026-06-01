"""FastAPI dependencies for the Workflow Repository Protocols.

Lift I-Repo-Workflow. One dependency function per Workflow Repository
Protocol; each constructs the concrete :class:`SqlAlchemy*Repository` from
the request's session. REST handlers depend on the Protocol via
``Depends(get_*_repository)``; tests override via
``app.dependency_overrides`` without touching the session.

Module name is leading-underscore-prefixed so it's clearly an internal
shared dependency module (not a router). Centralising the DI here avoids
cross-router import edges (``runs.py`` would otherwise have to import
``checkpoints.py`` just for ``get_run_repository``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.workflow.domain.repositories import (
    DecisionRepository,
    DeliverableRepository,
    RunRepository,
    SafeModeQueueRepository,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDecisionRepository,
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
    SqlAlchemySafeModeQueueRepository,
)


def get_run_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRepository:
    """One :class:`RunRepository` per request scope, backed by the request session."""
    return SqlAlchemyRunRepository(session)


def get_decision_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DecisionRepository:
    """One :class:`DecisionRepository` per request scope, backed by the request session."""
    return SqlAlchemyDecisionRepository(session)


def get_deliverable_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DeliverableRepository:
    """One :class:`DeliverableRepository` per request scope, backed by the request session."""
    return SqlAlchemyDeliverableRepository(session)


def get_safe_mode_queue_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SafeModeQueueRepository:
    """One :class:`SafeModeQueueRepository` per request scope, backed by the request session."""
    return SqlAlchemySafeModeQueueRepository(session)


__all__ = [
    "get_decision_repository",
    "get_deliverable_repository",
    "get_run_repository",
    "get_safe_mode_queue_repository",
]
