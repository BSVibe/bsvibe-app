"""Repository Protocols — application-layer seam onto Router persistence.

v8 §22 #11 + D44/D45. The Router context's two aggregates exposed here
(:class:`ModelAccountRepository` + :class:`RunRoutingRuleRepository`) cover
the per-workspace LLM account roster and the per-workspace run-routing rule
set. Application code (REST handlers, dispatch strategies, the tier-default
resolver, the agent_runner cross-reference from the Workflow context) depends
on the Protocols here, not on ``sqlalchemy.select``.

Concrete implementations live in
:mod:`backend.router.infrastructure.repositories`.

Pragmatic choice: the Repositories return the existing ORM row types
(:class:`~backend.router.accounts.models.ModelAccount`,
:class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow`) rather
than separate plain-Python domain entities — the architectural seam
(application code depending on a Protocol, not on ``sqlalchemy.select``) is
what reduces the v8 §22 #11 violation count. A future split-domain pass can
introduce dataclass entities without touching the Protocol shape.
"""

from __future__ import annotations

from backend.router.domain.repositories.model_account_repository import (
    ModelAccountRepository,
)
from backend.router.domain.repositories.run_routing_rule_repository import (
    RunRoutingRuleRepository,
)

__all__ = [
    "ModelAccountRepository",
    "RunRoutingRuleRepository",
]
