# bsvibe:stable-internal — modifications require a design doc update.
# Owners: workflow/domain/repositories
"""Repository Protocols — application-layer seam onto Workflow persistence.

v8 §22 #11 + D44/D45. The application layer depends on the Protocols here,
not on SQLAlchemy directly. Concrete implementations live in
:mod:`backend.workflow.infrastructure.repositories`.

The first per-context Repository extraction (Lift I-Repo-Workflow) shipped
:class:`RunRepository` + :class:`DecisionRepository`. Lift I-Repo-Workflow-2
adds :class:`DeliverableRepository` + :class:`SafeModeQueueRepository`. Lift
I-Repo-Workflow-3 closes the Workflow-context Repository pass with
:class:`RequestRepository` + :class:`IdempotencyRepository`. Further
repositories (VerificationResult / WorkStep / RunAttempt — execution-detail
rows) are deferred to a future sub-lift in the Router context.

Pragmatic choice: the Repositories return the existing ORM row types
(:class:`~backend.workflow.infrastructure.db.ExecutionRun`,
:class:`~backend.workflow.infrastructure.db.Decision`) rather than separate
plain-Python domain entities. The architectural seam — application code
depending on a Protocol, not on ``sqlalchemy.select`` — is what reduces the
v8 §22 #11 violation count. A future split-domain pass can introduce
dataclass entities without touching the Protocol shape.
"""

from __future__ import annotations

from backend.workflow.domain.repositories.decision_repository import DecisionRepository
from backend.workflow.domain.repositories.deliverable_repository import (
    DeliverableRepository,
)
from backend.workflow.domain.repositories.idempotency_repository import (
    IdempotencyRepository,
)
from backend.workflow.domain.repositories.request_repository import RequestRepository
from backend.workflow.domain.repositories.run_repository import RunRepository
from backend.workflow.domain.repositories.safe_mode_queue_repository import (
    SafeModeQueueRepository,
)

__all__ = [
    "DecisionRepository",
    "DeliverableRepository",
    "IdempotencyRepository",
    "RequestRepository",
    "RunRepository",
    "SafeModeQueueRepository",
]
