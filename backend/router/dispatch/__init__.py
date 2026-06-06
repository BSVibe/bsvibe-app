"""Legacy dispatch error surface (Lift E2 — classifier removed).

Before Lift E2 this module hosted :class:`GatewayDispatcher`,
:class:`DispatchRequest`, :class:`DispatchResult` and a classifier-driven
chain that routed every LLM call through a tier verdict. Lift E2 deletes
that entire path; routing is now a single mechanism:
:class:`backend.dispatch.resolver.ModelAccountResolver` — rule match →
workspace default → :class:`backend.dispatch.resolver.NoMatchingRouteError`.

The two error types in this module stay because the OpenAI-shape proxy
(:mod:`backend.api.v1.chat`) raises and translates them; every other
caller has moved to the new dispatch context under :mod:`backend.dispatch`.
"""

from __future__ import annotations


class DispatchError(RuntimeError):
    """Catch-all for unrecoverable LLM dispatch errors."""


class ModelAccountNotFound(DispatchError):
    """The (workspace, account, model_account) row is missing / inactive."""


__all__ = ["DispatchError", "ModelAccountNotFound"]
