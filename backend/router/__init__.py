"""Router context — unified module.

Owns model accounts and the LiteLLM wrapper. The budget subsystem was deleted 2026-08-20: its policy table
held 0 rows in prod with no authoring surface, and its tracker store was
rebuilt per request, so ``BudgetExceeded`` could never fire. After Lift E2 the classifier / tier vocabulary
is gone — routing flows through :mod:`backend.dispatch` (resolver +
adapter) per founder policy ``bsvibe-no-implicit-routing``.

Public surface (union):

- :mod:`backend.router.accounts` — ``ModelAccount`` entity + CRUD.
- :mod:`backend.router.llm_client` — folded ``bsvibe-llm`` wrapper.

The infrastructure / domain repositories and the routing run-routing
internals (engine + DB rows) are **private** — callers depend on the
Protocol surface re-exported here and never reach into the SQL adapter
or the rule-evaluation table directly.
"""

from __future__ import annotations

from backend.router.llm_client import LlmClient, LlmResponse

# ``backend.router.dispatch`` (``DispatchError`` / ``ModelAccountNotFound``) was deleted on
# 2026-10-16 with its last raiser, the ``/api/v1/chat/completions`` proxy.
__all__ = [
    "LlmClient",
    "LlmResponse",
]
