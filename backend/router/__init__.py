"""Router context — unified module (Lift A facade + Lift B gateway/accounts merge).

Lift A introduced the public facade Protocol (``Router``) plus the
``LlmRequest`` / ``LlmResult`` / ``LlmRoutingHints`` dataclasses — the surface
callers will depend on once the dispatch path is rewired.

Lift B renamed the existing ``backend.gateway`` package to ``backend.router``
and folded the previously-hoisted ``backend.accounts`` back in as
``backend.router.accounts``. The pre-existing public API of those packages
is preserved verbatim — no caller behavior changes in this lift; subsequent
lifts (E…N) switch call sites to the Router Protocol.

Public surface (union):

- :mod:`backend.router.facade` — Router Protocol + LLM I/O dataclasses (Lift A).
- :mod:`backend.router.accounts` — ``ModelAccount`` entity + CRUD (lifted from
  the former top-level ``backend.accounts``).
- :mod:`backend.router.budget`     — ``BudgetPolicyService`` + tracker.
- :mod:`backend.router.classifier` — static + 2-tier (local vs cloud).
- :mod:`backend.router.dispatch`   — ``GatewayDispatcher`` + request/result types.
- :mod:`backend.router.llm_client` — folded ``bsvibe-llm`` wrapper.
"""

from __future__ import annotations

from backend.router import budget, classifier
from backend.router.dispatch import (
    DispatchError,
    DispatchRequest,
    DispatchResult,
    GatewayDispatcher,
    ModelAccountNotFound,
)
from backend.router.facade import (
    LlmRequest,
    LlmResult,
    LlmRoutingHints,
    Router,
)
from backend.router.llm_client import LlmClient, LlmResponse

__all__ = [
    "DispatchError",
    "DispatchRequest",
    "DispatchResult",
    "GatewayDispatcher",
    "LlmClient",
    "LlmRequest",
    "LlmResponse",
    "LlmResult",
    "LlmRoutingHints",
    "ModelAccountNotFound",
    "Router",
    "budget",
    "classifier",
]
