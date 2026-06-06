"""ModelAccount adapter-selection predicates (Lift E2).

Replaces the deleted ``backend.router.dispatch.strategies`` module. The
only piece of that module that survived E2 is the provider-name → adapter
discriminator: ``provider == "executor"`` selects the worker-RPC
adapter; everything else routes through the LiteLLM adapter. This is a
plain label-to-adapter mapping, **not** a tier/classifier verdict — per
founder policy ``bsvibe-no-implicit-routing``.

The constant + predicate live on the accounts side (one folder up from
the deleted dispatch path) because they are a pure account-introspection
helper, not a dispatch decision.
"""

from __future__ import annotations

from backend.router.accounts.models import ModelAccount

#: Provider value marking an executor (worker / CLI) ModelAccount. SQL
#: filters compare against this literal directly so the column query is
#: explicit at every call site; predicate-level checks (which adapter? is
#: this row routable through the LiteLLM SDK?) call :func:`is_executor_account`.
EXECUTOR_PROVIDER: str = "executor"


def is_executor_account(account: ModelAccount) -> bool:
    """True when ``account`` routes to the executor (worker) adapter.

    The only adapter-selection helper after Lift E2. There is no "tier" or
    "classifier" verdict layered on top — every other field of the account
    (label, litellm_model, jurisdiction, …) flows through unchanged.
    """
    return account.provider == EXECUTOR_PROVIDER


__all__ = ["EXECUTOR_PROVIDER", "is_executor_account"]
