"""BSVibe gateway — LLM dispatch, multi-account routing, usage tracking.

Lifted from BSGateway role code with a deep refactor: every request is
now scoped to ``(workspace_id, account_id)`` instead of just
``workspace_id``, so a single workspace can hold many ModelAccount rows
(per provider, per region, per budget) and each carries its own routing
+ budget envelope.

Public surface for Bundle 1 is the dispatch entry point plus the four
sub-packages it composes:

- :mod:`backend.gateway.accounts` — ``ModelAccount`` entity + CRUD.
- :mod:`backend.gateway.budget`   — ``BudgetPolicyService`` + tracker.
- :mod:`backend.gateway.classifier` — static + 2-tier (local vs cloud).
- :mod:`backend.gateway.llm_client` — folded ``bsvibe-llm`` wrapper.

The big BSGateway pieces — embedding-based routing, rules CRUD, MCP
admin tools, executor worker dispatch — are deferred to a follow-up
bundle once the orchestrator/workers track lands and we know what the
caller surface needs to look like inside the monorepo.
"""

from __future__ import annotations

from backend.gateway import accounts, budget, classifier
from backend.gateway.dispatch import (
    DispatchError,
    DispatchRequest,
    DispatchResult,
    GatewayDispatcher,
    ModelAccountNotFound,
)
from backend.gateway.llm_client import LlmClient, LlmResponse

__all__ = [
    "DispatchError",
    "DispatchRequest",
    "DispatchResult",
    "GatewayDispatcher",
    "LlmClient",
    "LlmResponse",
    "ModelAccountNotFound",
    "accounts",
    "budget",
    "classifier",
]
