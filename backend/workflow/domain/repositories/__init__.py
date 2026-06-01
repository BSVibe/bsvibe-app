"""Repository Protocols — application-layer seam onto Workflow persistence.

v8 §22 #11 + D44/D45. The application layer depends on the Protocols here,
not on SQLAlchemy directly. Concrete implementations live in
:mod:`backend.workflow.infrastructure.repositories`.

The first per-context Repository extraction (Lift I-Repo-Workflow). This lift
ships the two highest-violation Repositories — :class:`RunRepository` and
:class:`DecisionRepository`. Further repositories (Deliverable, SafeModeQueue,
Request, Idempotency) are deferred to follow-up sub-lifts.

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
from backend.workflow.domain.repositories.run_repository import RunRepository

__all__ = ["DecisionRepository", "RunRepository"]
