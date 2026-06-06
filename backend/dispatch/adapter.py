"""ModelAccountAdapter — uniform call-site interface.

A call site that needs an LLM asks
:class:`~backend.dispatch.resolver.ModelAccountResolver` for an account
and then calls one verb on the adapter the resolver hands back:

* :meth:`ModelAccountAdapter.chat` — system + messages + tools → response.

Two concrete adapters land in this module:

* :class:`LiteLLMAdapter` wraps :class:`~backend.router.llm_client.LlmClient`
  for any provider whose dispatch is a direct provider-API chat call
  (anthropic, openai, ollama_chat, …).
* :class:`ExecutorAdapter` is the worker / CLI branch (``provider="executor"``).
  Lift E3 swaps the body for a direct subprocess call against the worker's
  ``execute`` stream-json shape; Lift E2 leaves it as a stub that raises
  ``NotImplementedError`` so an executor account is loud, not silent.

Both adapters expose the same ``chat`` surface.  ``supported_methods`` is
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
from backend.router.accounts.predicates import is_executor_account
from backend.router.llm_client import LlmClient, LlmResponse

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


#: OpenAI-style message — ``{"role": ..., "content": ..., ...}``. Plain
#: ``dict`` so the adapter forwarding is zero-cost (call sites already
#: speak this shape).
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

    ``raw`` carries the underlying provider's response object so a power-
    user call site (the agent loop's token-usage telemetry, say) can
    inspect it. Most call sites only touch ``content`` + ``tool_calls``.
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
    """The single verb dispatch hands to call sites — ``chat``.

    A concrete implementation MUST advertise its supported methods on the
    ``supported_methods`` attribute so rule creation can validate that the
    caller's ``required_methods`` is a subset. Today every adapter
    supports ``{"chat"}``.
    """

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

    Calls :class:`LlmClient` directly with the account's decrypted
    credentials. The credential never leaves this module — it is held on
    the dataclass and threaded into the LLM call's ``api_key`` slot, not
    logged anywhere.
    """

    account: ModelAccount
    api_key: str
    llm: LlmClient
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
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
# Executor adapter — E3 will wire it to the subprocess worker; E2 stub.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutorAdapter:
    """Worker / CLI adapter for ``provider="executor"`` accounts.

    Lift E2 ships the dataclass + ``supported_methods`` so rule creation
    can validate against this shape, but the verb itself raises until
    Lift E3 wires the subprocess executor (claude_code / codex /
    opencode) directly. The OLD path (classifier + tier vocabulary) is
    gone — there is no silent fallback through the legacy gateway, per
    founder policy ``bsvibe-no-implicit-routing``.
    """

    account: ModelAccount
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    supported_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        del system, messages, tools
        raise NotImplementedError(
            "ExecutorAdapter.chat lands in Lift E3 — until then resolver hits on "
            "an executor account must be handled by the ExecutorOrchestrator "
            "branch upstream (see backend.workflow.application.runtime.agent_runtime)"
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def adapter_for(
    account: ModelAccount,
    *,
    session: AsyncSession,
    settings: Settings,
    api_key: str,
    llm: LlmClient | None = None,
) -> ModelAccountAdapter:
    """Pick the right :class:`ModelAccountAdapter` for an account.

    Executor accounts (per :func:`is_executor_account`) →
    :class:`ExecutorAdapter`. Anything else → :class:`LiteLLMAdapter`.

    ``api_key`` is the already-decrypted key the caller pulled from
    :meth:`backend.router.accounts.service.ModelAccountService.reveal_api_key`;
    we never decrypt inside the adapter so credentials never leak
    through logs emitted from this module.

    ``session`` + ``settings`` are unused today but kept on the call site
    surface so a later lift can wire them without breaking callers.
    """
    del session, settings  # reserved for future per-account budget hooks
    if is_executor_account(account):
        return ExecutorAdapter(
            account=account,
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
    return LiteLLMAdapter(
        account=account,
        api_key=api_key,
        llm=llm or LlmClient(),
        workspace_id=account.workspace_id,
        account_id=account.account_id,
        model_account_id=account.id,
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
