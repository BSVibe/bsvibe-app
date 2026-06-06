"""Router context — unified module.

Owns model accounts, budget policy, the LiteLLM wrapper, and the LLM
dispatch error surface. After Lift E2 the classifier / tier vocabulary
is gone — routing flows through :mod:`backend.dispatch` (resolver +
adapter) per founder policy ``bsvibe-no-implicit-routing``.

Public surface (union):

- :mod:`backend.router.facade` — Router Protocol + LLM I/O dataclasses.
- :mod:`backend.router.accounts` — ``ModelAccount`` entity + CRUD.
- :mod:`backend.router.budget` — ``BudgetPolicyService`` + tracker.
- :mod:`backend.router.dispatch` — dispatch error types.
- :mod:`backend.router.llm_client` — folded ``bsvibe-llm`` wrapper.

The infrastructure / domain repositories and the routing run-routing
internals (engine + DB rows) are **private** — callers depend on the
Protocol surface re-exported here and never reach into the SQL adapter
or the rule-evaluation table directly.
"""

from __future__ import annotations

from backend.router import budget
from backend.router.dispatch import DispatchError, ModelAccountNotFound
from backend.router.facade import (
    LlmRequest,
    LlmResult,
    LlmRoutingHints,
    Router,
)
from backend.router.llm_client import LlmClient, LlmResponse

__all__ = [
    "DispatchError",
    "LlmClient",
    "LlmRequest",
    "LlmResponse",
    "LlmResult",
    "LlmRoutingHints",
    "ModelAccountNotFound",
    "Router",
    "budget",
]
