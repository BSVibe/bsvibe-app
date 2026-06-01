"""SQLAlchemy concrete implementations of the Workflow Repository Protocols.

v8 D44/D45 — infrastructure-layer code IS allowed to import sqlalchemy
directly. Each ``SqlAlchemy*Repository`` adapts the corresponding Protocol
declared in :mod:`backend.workflow.domain.repositories` to one ``AsyncSession``.
"""

from __future__ import annotations

from backend.workflow.infrastructure.repositories.decision_repository_sql import (
    SqlAlchemyDecisionRepository,
)
from backend.workflow.infrastructure.repositories.deliverable_repository_sql import (
    SqlAlchemyDeliverableRepository,
)
from backend.workflow.infrastructure.repositories.idempotency_repository_sql import (
    SqlAlchemyIdempotencyRepository,
)
from backend.workflow.infrastructure.repositories.request_repository_sql import (
    SqlAlchemyRequestRepository,
)
from backend.workflow.infrastructure.repositories.run_repository_sql import SqlAlchemyRunRepository
from backend.workflow.infrastructure.repositories.safe_mode_queue_repository_sql import (
    SqlAlchemySafeModeQueueRepository,
)

__all__ = [
    "SqlAlchemyDecisionRepository",
    "SqlAlchemyDeliverableRepository",
    "SqlAlchemyIdempotencyRepository",
    "SqlAlchemyRequestRepository",
    "SqlAlchemyRunRepository",
    "SqlAlchemySafeModeQueueRepository",
]
