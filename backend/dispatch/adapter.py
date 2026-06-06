"""ModelAccountAdapter — uniform call-site interface (Lift E1).

A call site that has been migrated to the new dispatch path no longer
constructs a :class:`~backend.router.dispatch.GatewayDispatcher` directly;
it asks :class:`~backend.dispatch.resolver.ModelAccountResolver` for an
account and then calls one verb on the adapter the resolver hands back:

* :meth:`ModelAccountAdapter.chat` — system + messages + tools → response.

Two concrete adapters land in E1:

* :class:`LiteLLMAdapter` wraps :class:`~backend.router.llm_client.LlmClient`
  for any provider whose dispatch is a direct provider-API chat call
  (anthropic, openai, ollama_chat, …). It is the canonical happy path.
* :class:`ExecutorAdapter` is a **placeholder** that delegates back to the
  existing classifier-based gateway dispatcher so the new code path can
  ship even before E3 wires the subprocess executor (claude_code / codex
  / opencode) directly. Once E3 lands, this class wraps
  :class:`~backend.executors.worker.claude_code.ClaudeCodeExecutor` &
  friends — keeping the call-site API stable.

Both adapters expose the same ``chat`` shape. ``supported_methods`` is
checked at rule-creation time so an incompatible binding fails fast.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.router.accounts.models import ModelAccount
from backend.router.dispatch import DispatchRequest, GatewayDispatcher
from backend.router.dispatch.strategies import is_executor_account
from backend.router.llm_client import LlmClient, LlmResponse
from backend.workflow.application.runtime.dispatcher import build_gateway_dispatcher

logger = structlog.get_logger(__name__)

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "ChatToolCall",
    "ExecutorAdapter",
    "LiteLLMAdapter",
    "ModelAccountAdapter",
    "adapter_for",
]


# ---------------------------------------------------------------------------
# Wire shapes — the only adapter surface the call sites see.
# ---------------------------------------------------------------------------


#: OpenAI-style message — ``{"role": "system"|"user"|"assistant"|"tool",
#: "content": str, ...}``. We use a plain ``dict`` rather than a Pydantic
#: model to keep the adapter forwarding zero-cost; the call sites already
#: speak this shape.
ChatMessage = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatToolCall:
    """A single tool call the model emitted, normalized."""

    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """The minimum useful shape every call site needs from a chat turn.

    ``raw`` is intentionally typed as ``Any`` — it carries the underlying
    provider's response object so a power-user call site (the agent loop's
    token-usage telemetry, say) can inspect it. Most call sites only touch
    ``content`` + ``tool_calls``.
    """

    content: str
    tool_calls: tuple[ChatToolCall, ...] = ()
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    raw: Any = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelAccountAdapter(Protocol):
    """The single verb dispatch hands to call sites in E1 — ``chat``.

    A concrete implementation MUST advertise its supported methods on the
    ``supported_methods`` attribute so rule creation can validate that the
    caller's ``required_methods`` is a subset. Today every adapter
    supports ``{"chat"}``; a future ``execute`` verb (for a different
    integration shape — out of scope for E1) would be additive.
    """

    #: The set of adapter-method names this implementation honors. Today
    #: ``frozenset({"chat"})`` — the only verb in E1.
    supported_methods: frozenset[str]

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Single chat-completion turn.

        ``system`` is the system prompt; the adapter prepends it as
        ``{"role": "system", "content": system}`` so the call site does
        not have to remember whether the underlying provider treats the
        system prompt as a kwarg or a message slot.

        ``tools`` is the OpenAI-style tools schema — ``None`` for the
        plain chat path. When the model emits tool calls, the adapter
        surfaces them in ``response.tool_calls`` and the workflow
        dispatches them; the adapter does NOT execute tools on its own.
        """


# ---------------------------------------------------------------------------
# LiteLLM-backed adapter (the canonical happy path).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LiteLLMAdapter:
    """Wraps :class:`~backend.router.llm_client.LlmClient` for one account.

    Two paths exist in E1 while classifier / budget are still alive:

    * If ``dispatcher`` is provided, ``chat`` routes through the existing
      :class:`GatewayDispatcher` so budget / classifier still run. This
      is the default in production — bigger E2 cuts the classifier.
    * If ``dispatcher`` is ``None`` (set-up by ``adapter_for`` only when
      the caller explicitly opts out, e.g. in tests), ``chat`` calls
      :class:`LlmClient` directly with the account's decrypted
      credentials — bypassing budget. The seam exists so the test layer
      and (eventually) E2 can shed the classifier without rewriting every
      adapter.
    """

    account: ModelAccount
    api_key: str
    llm: LlmClient
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    dispatcher: GatewayDispatcher | None = None
    #: The :class:`ClassificationFeatures` to forward to the dispatcher.
    #: Today this is a per-caller fingerprint; E2 deletes the field
    #: entirely along with the classifier. Held as a plain ``Any`` here
    #: so this module does NOT have to import the classifier package
    #: while the seam is in transition.
    legacy_features: Any = None
    legacy_projected_cost_cents: int = 1
    supported_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        full_messages: list[ChatMessage] = [{"role": "system", "content": system}]
        full_messages.extend(dict(m) for m in messages)

        if self.dispatcher is not None and self.legacy_features is not None:
            # Production path while E2 is pending — keep budget + classifier alive.
            request = DispatchRequest(
                workspace_id=self.workspace_id,
                account_id=self.account_id,
                model_account_id=self.model_account_id,
                messages=full_messages,
                features=self.legacy_features,
                projected_cost_cents=self.legacy_projected_cost_cents,
                tools=tools,
            )
            result = await self.dispatcher.dispatch(request)
            return _from_llm_response(result.response)

        # Direct path — used by tests today; will become the production
        # default once E2 lands. The provider key flows through the
        # adapter, never logged.
        response = await self.llm.chat(
            model=self.account.litellm_model,
            messages=full_messages,
            api_base=self.account.api_base,
            api_key=self.api_key,
            extra_params=dict(self.account.extra_params),
            tools=tools,
        )
        return _from_llm_response(response)


# ---------------------------------------------------------------------------
# Executor placeholder adapter (E3 wraps subprocess executors here).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutorAdapter:
    """Placeholder for executor (``provider="executor"``) accounts.

    E1 does not migrate the subprocess executor path; E3 does. While the
    classifier-based gateway is still wired, this adapter routes the
    chat call through :class:`GatewayDispatcher` exactly as a native
    account would — which today silently falls back to the classifier's
    tier verdict for an executor account. The placeholder exists so call
    sites can already be written against the new resolver/adapter API;
    when E3 swaps the body for a direct subprocess call (NDJSON
    stream-json from claude_code / codex / opencode) the call sites do
    not change.
    """

    account: ModelAccount
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    dispatcher: GatewayDispatcher | None = None
    legacy_features: Any = None
    legacy_projected_cost_cents: int = 1
    supported_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        if self.dispatcher is None or self.legacy_features is None:
            raise NotImplementedError(
                "ExecutorAdapter direct path lands in Lift E3; today it requires "
                "the legacy GatewayDispatcher seam to be passed in via adapter_for"
            )
        full_messages: list[ChatMessage] = [{"role": "system", "content": system}]
        full_messages.extend(dict(m) for m in messages)
        request = DispatchRequest(
            workspace_id=self.workspace_id,
            account_id=self.account_id,
            model_account_id=self.model_account_id,
            messages=full_messages,
            features=self.legacy_features,
            projected_cost_cents=self.legacy_projected_cost_cents,
            tools=tools,
        )
        result = await self.dispatcher.dispatch(request)
        return _from_llm_response(result.response)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def adapter_for(
    account: ModelAccount,
    *,
    session: AsyncSession,
    settings: Settings,
    api_key: str,
    legacy_features: Any = None,
    legacy_projected_cost_cents: int = 1,
    llm: LlmClient | None = None,
    dispatcher: GatewayDispatcher | None = None,
) -> ModelAccountAdapter:
    """Pick the right :class:`ModelAccountAdapter` for an account.

    Executor accounts (per :func:`is_executor_account`) → :class:`ExecutorAdapter`
    placeholder. Anything else → :class:`LiteLLMAdapter`.

    ``api_key`` is the already-decrypted key the caller pulled from
    :meth:`backend.router.accounts.service.ModelAccountService.reveal_api_key`;
    we never decrypt inside the adapter so credentials never leak through
    logs or error messages emitted from this module.

    ``legacy_features`` + ``dispatcher`` keep the path-through to the
    classifier-based gateway alive for E1 — E2 strips them both.
    """
    actual_dispatcher = dispatcher or build_gateway_dispatcher(session, settings)
    if is_executor_account(account):
        return ExecutorAdapter(
            account=account,
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
            dispatcher=actual_dispatcher,
            legacy_features=legacy_features,
            legacy_projected_cost_cents=legacy_projected_cost_cents,
        )
    return LiteLLMAdapter(
        account=account,
        api_key=api_key,
        llm=llm or LlmClient(),
        workspace_id=account.workspace_id,
        account_id=account.account_id,
        model_account_id=account.id,
        dispatcher=actual_dispatcher,
        legacy_features=legacy_features,
        legacy_projected_cost_cents=legacy_projected_cost_cents,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _from_llm_response(response: LlmResponse) -> ChatResponse:
    tool_calls = tuple(
        ChatToolCall(
            id=str(call.get("id") or ""),
            name=str(call.get("function", {}).get("name") or ""),
            arguments_json=str(call.get("function", {}).get("arguments") or ""),
        )
        for call in response.tool_calls
    )
    return ChatResponse(
        content=response.content,
        tool_calls=tool_calls,
        usage_prompt_tokens=response.usage_prompt_tokens,
        usage_completion_tokens=response.usage_completion_tokens,
        raw=response.raw,
    )
