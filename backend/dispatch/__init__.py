"""BSVibe dispatch — Lift E1.

The dispatch context owns the new mechanism-only routing surface:

* :class:`~backend.dispatch.caller_registry.CallerSpec` — a call-site's
  declaration of its required adapter methods (today: ``{"chat"}``).
* :class:`~backend.dispatch.adapter.ModelAccountAdapter` — uniform Protocol
  the resolver hands back to call sites. ``chat(system, messages, tools)``
  is the only verb in E1; ``LiteLLMAdapter`` wraps the existing
  :class:`~backend.router.llm_client.LlmClient`; ``ExecutorAdapter`` is a
  placeholder that delegates back to the existing classifier-based
  :class:`~backend.router.dispatch.GatewayDispatcher` until E3 wraps the
  subprocess executor directly.
* :class:`~backend.dispatch.resolver.ModelAccountResolver` — looks up the
  account for ``(caller_id, workspace_id)``: first an active
  :class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow`, then
  the workspace's ``default_account_id`` fallback, then a hard
  :class:`~backend.dispatch.resolver.NoMatchingRouteError`.

This lift introduces the new path in parallel; the existing classifier +
tier + provider-allowlist plumbing in :mod:`backend.router.classifier`,
:mod:`backend.router.dispatch.strategies`, and
:mod:`backend.router.routing.run_routing.tier_default` is left intact and
marked deprecated. Lift E2 removes it.

Design source: ``~/Docs/BSVibe_Dispatch_Redesign_2026-06-05.md``.
Founder policy: ``feedback_bsvibe_no_implicit_routing``.
"""

from __future__ import annotations

from backend.dispatch.adapter import (
    ChatMessage,
    ChatResponse,
    ChatToolCall,
    ExecutorAdapter,
    LiteLLMAdapter,
    ModelAccountAdapter,
    adapter_for,
)
from backend.dispatch.caller_registry import (
    KNOWN_CALLERS,
    CallerSpec,
    get_caller_spec,
    list_all_callers,
)
from backend.dispatch.resolver import (
    ModelAccountResolver,
    NoMatchingRouteError,
    ResolvedAccount,
)

__all__ = [
    "KNOWN_CALLERS",
    "CallerSpec",
    "ChatMessage",
    "ChatResponse",
    "ChatToolCall",
    "ExecutorAdapter",
    "LiteLLMAdapter",
    "ModelAccountAdapter",
    "ModelAccountResolver",
    "NoMatchingRouteError",
    "ResolvedAccount",
    "adapter_for",
    "get_caller_spec",
    "list_all_callers",
]
