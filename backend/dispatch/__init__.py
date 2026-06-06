"""BSVibe dispatch — the mechanism-only routing surface (Lift E2).

* :class:`~backend.dispatch.caller_registry.CallerSpec` — a call-site's
  declaration of its required adapter methods (today: ``{"chat"}``).
* :class:`~backend.dispatch.adapter.ModelAccountAdapter` — uniform Protocol
  the resolver hands back to call sites. ``chat(system, messages, tools)``
  is the only verb; :class:`LiteLLMAdapter` wraps
  :class:`~backend.router.llm_client.LlmClient`;
  :class:`ExecutorAdapter` (Lift E3) dispatches a single-shot CLI
  subprocess task through the existing
  :mod:`backend.executors.dispatch` substrate (worker stream XADD →
  ``claude --print`` / ``codex -p`` / ``opencode -p`` → result POST →
  :class:`ChatResponse`).
* :class:`~backend.dispatch.resolver.ModelAccountResolver` — looks up the
  account for ``(caller_id, workspace_id)``: first an active
  :class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow`, then
  the workspace's ``default_account_id`` fallback, then a hard
  :class:`~backend.dispatch.resolver.NoMatchingRouteError`.

Lift E2 removed the classifier / tier / provider-allow-list plumbing
(``backend.router.classifier``, ``backend.router.dispatch.strategies``,
``backend.router.routing.run_routing.tier_default``,
``backend.router.routing.run_routing.multi_account``). The new path is
the only path.

Design source: ``~/Docs/BSVibe_Dispatch_Redesign_2026-06-05.md``.
Founder policy: ``feedback_bsvibe_no_implicit_routing``.
"""

from __future__ import annotations

from backend.dispatch.adapter import (
    ChatMessage,
    ChatResponse,
    ChatToolCall,
    ExecutorAdapter,
    ExecutorAdapterUnavailable,
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
    "ExecutorAdapterUnavailable",
    "LiteLLMAdapter",
    "ModelAccountAdapter",
    "ModelAccountResolver",
    "NoMatchingRouteError",
    "ResolvedAccount",
    "adapter_for",
    "get_caller_spec",
    "list_all_callers",
]
