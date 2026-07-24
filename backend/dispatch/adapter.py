"""ModelAccountAdapter — uniform call-site interface.

A call site that needs an LLM asks
:class:`~backend.dispatch.resolver.ModelAccountResolver` for an account
and then calls one verb on the adapter the resolver hands back:

* :meth:`ModelAccountAdapter.chat` — system + messages + tools → response.

Two concrete adapters live in this module:

* :class:`LiteLLMAdapter` wraps :class:`~backend.router.llm_client.LlmClient`
  for any provider whose dispatch is a direct provider-API chat call
  (anthropic, openai, ollama_chat, …).
* :class:`ExecutorAdapter` is the worker / CLI branch (``provider="executor"``)
  — Lift E3 wires it to the existing CLI subprocess executor by enqueueing
  a single-shot chat task onto the worker's Redis stream and awaiting the
  worker's POSTed result. The worker runs ``claude --print`` /
  ``codex -p`` / ``opencode -p`` — same ``execute(prompt, context)`` streamers (claude_code.py /
  codex.py / opencode.py), same ``sanitized_subprocess_env``, same
  per-line deadline + rate-limit retry semantics — but for a single chat
  turn instead of a whole run. The CLI's response text becomes
  :attr:`ChatResponse.content`.

Both adapters expose the same ``chat`` surface.  ``supported_methods`` is
checked at rule-creation time so an incompatible binding fails fast.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import is_executor_account
from backend.router.llm_client import LlmClient, LlmResponse
from backend.workflow.application.tool_registry import WORK_TOOL_MCP_NAMES

logger = structlog.get_logger(__name__)

__all__ = [
    "ChatMessage",
    "ChatResponse",
    "ChatToolCall",
    "ExecutorAdapter",
    "ExecutorAdapterUnavailable",
    "ExecutorCapacitySaturated",
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
    #: Per-caller request timeout in seconds (Lift E9). Settable so a call site
    #: can tighten the bound for its context — e.g. the inline Direct answer
    #: caps the synchronous HTTP wait below the default frame timeout. ``None``
    #: leaves each adapter's own default in effect.
    timeout_s: float | None

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


#: Which executor CLIs can be handed BSVibe's OWN tools over MCP (T2).
#:
#: The founder's principle: the executor is the user's LLM CLIENT, not an execution
#: environment. State lives on the server, so an agent must act through BSVibe's tools
#: (:mod:`backend.mcp.tools.work_tools`), executed server-side — not through the CLI's own
#: local tools in a temp dir the worker scrapes back. That old shape is what let an agent
#: invent a codebase in an empty dir and ship it, zero a >256 KB file, and lose an entire
#: result on a deletion (parity audit, 2026-07-14).
#:
#: ``claude_code`` is verified against the real binary: it accepts an HTTP MCP server with
#: auth headers and restricts the model to exactly the tools we allow. The others are not —
#: and a CLI's contract is not something to guess (that is how wrappers rot). Until each is
#: verified, agentic work routed to it is REFUSED rather than quietly run in the old shape:
#: "which account did I route?" must not change what the product does.
_REMOTE_TOOL_EXECUTORS: frozenset[str] = frozenset({"claude_code"})


#: The tools an executor agent may use — BSVibe's, over MCP. The CLI is given exactly these
#: names and nothing else, and the worker verifies the CLI's own init event against them: an
#: enumerated denylist of the vendor's built-ins rots, so the allowlist is what we check.
#:
#: DERIVED, never hand-maintained (INV-7 #2): it IS the set of ``bsvibe_work_*`` tools the MCP
#: server registers (``register_work_tools`` builds those from the SAME
#: ``WORK_TOOL_FORWARDING_SPECS``). Advertising a tool the server doesn't register — or the
#: reverse — is therefore impossible by construction, not merely tested-against. This closed the
#: ``knowledge_search`` / ``declare_verification`` / ``file_edit`` drifts where a hand-kept list
#: silently disagreed with the real surface.
WORK_TOOL_NAMES: tuple[str, ...] = WORK_TOOL_MCP_NAMES


async def _founder_of(session: Any, workspace_id: uuid.UUID) -> uuid.UUID:
    """The workspace's member — every BSVibe workspace is one founder (v1 product reality)."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.db import MembershipRow  # noqa: PLC0415

    row = (
        (
            await session.execute(
                select(MembershipRow).where(MembershipRow.workspace_id == workspace_id).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise ExecutorAdapterUnavailable(
            f"workspace {workspace_id} has no member — cannot scope a task token to anyone"
        )
    return uuid.UUID(str(row.user_id))


def build_work_tool_dispatch(*, token: str, issuer: str) -> dict[str, Any]:
    """The MCP surface a dispatched agentic task carries: BSVibe's tools, a run-scoped token.

    The run is NOT in the URL — it rides in the token's ``run_id`` claim, so an agent cannot
    redirect its writes by editing an argument (see :mod:`backend.mcp.tools.work_tools`).
    """
    config = {
        "mcpServers": {
            "bsvibe": {
                "type": "http",
                "url": f"{issuer.rstrip('/')}/mcp/",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return {
        "mcp_config": json.dumps(config),
        "allowed_tools": [f"mcp__bsvibe__{name}" for name in WORK_TOOL_NAMES],
    }


def supports_remote_tools(executor_type: str) -> bool:
    """Can this executor CLI be given BSVibe's tools (and stripped of its own)?"""
    return executor_type in _REMOTE_TOOL_EXECUTORS


class ExecutorAdapterUnavailable(RuntimeError):
    """Raised by :meth:`ExecutorAdapter.chat` when the chat cannot dispatch.

    Several distinct failure modes share one exception type because every
    call site treats them the same way (surface to the user / write a
    Decision, never a silent fallback):

    * No Redis client wired into the resolver (the worker stream needs it).
    * The executor account's ``extra_params`` is missing
      ``executor_type``.
    * No online worker carries the requested capability (Decision in the
      legacy full-run path; here we raise so the chat call site sees it).
    * A dispatched task came back ``failed`` (the worker's CLI exited non-zero).

    ``retryable`` marks the TRANSIENT modes — a dispatched task that came back
    ``failed`` (a momentary CLI/API/resource blip on the worker; live dogfood
    showed an executor ``exit 1`` recover on the very next call). The adapter
    re-dispatches those a bounded number of times. Config errors (no redis / no
    executor_type) and a genuine timeout are NOT retryable.
    """

    def __init__(self, *args: object, retryable: bool = False) -> None:
        super().__init__(*args)
        self.retryable = retryable


class ExecutorCapacitySaturated(ExecutorAdapterUnavailable):
    """Raised instead of blocking when a ``yield_on_saturation`` caller finds
    all live workers saturated; the run-drive caller catches it to yield-back
    (leave the run open, retry next poll).

    A SUBCLASS of :class:`ExecutorAdapterUnavailable` so every existing
    ``except ExecutorAdapterUnavailable`` still catches it — the planner's broad
    handler, the chat retry loop, the orchestrator's account-resolution guards.
    Only call sites that want to DISTINGUISH the yield-back (the AgentWorker's
    per-run loop, the product-tick planner's re-raise) name it explicitly.
    """


# ---------------------------------------------------------------------------
# Output-language — applied UNIFORMLY by every adapter.
# ---------------------------------------------------------------------------


def _system_with_output_language(system: str) -> str:
    """Append the workspace OUTPUT-language directive to a system prompt.

    The directive makes the model write user-facing prose in the workspace
    language (empty for English). It is a property of the OUTPUT, not of the
    TRANSPORT — so EVERY adapter (LiteLLM AND executor) applies it through this
    one helper, and a caller gets identical localization whichever account
    resolves. (Previously only the LiteLLM adapter localized, so the same
    chat-shaped caller wrote Korean on a LiteLLM account but English on an
    executor account — an abstraction leak this closes.)"""
    from backend.identity.output_language import language_directive  # noqa: PLC0415

    return system + language_directive()


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

    ``timeout_s`` (Lift E9) — per-caller request timeout in seconds.
    Threaded into the LiteLLM ``timeout`` kwarg (litellm accepts a
    per-request ``timeout`` knob and honours it across every provider).
    ``None`` leaves the kwarg out so litellm uses its own default;
    explicit values let chat-shaped callers (frame / judge / knowledge
    ingest, ~60-180 s) fail fast instead of waiting on the legacy
    1800 s ExecutorAdapter default.
    """

    account: ModelAccount
    api_key: str
    llm: LlmClient
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    timeout_s: float | None = None
    supported_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        full_messages: list[ChatMessage] = [
            {"role": "system", "content": _system_with_output_language(system)}
        ]
        full_messages.extend(dict(m) for m in messages)
        # Fold the per-caller timeout into ``extra_params`` so it lands in
        # ``litellm.acompletion``'s ``timeout`` kwarg (LiteLLM honours
        # request-level ``timeout`` across every provider). The account's
        # own ``extra_params`` takes precedence — operators can override
        # the per-caller default by setting ``extra_params.timeout`` on
        # the model account row itself.
        extra_params = dict(self.account.extra_params)
        if self.timeout_s is not None and "timeout" not in extra_params:
            extra_params["timeout"] = self.timeout_s
        response = await self.llm.chat(
            model=self.account.litellm_model,
            messages=full_messages,
            api_base=self.account.api_base,
            api_key=self.api_key,
            extra_params=extra_params,
            tools=tools,
        )
        return _from_llm_response(response)


# ---------------------------------------------------------------------------
# Executor adapter — thin wrapper around the existing subprocess dispatch
# substrate (`backend.executors.dispatch`). Single-shot CLI chat call.
# ---------------------------------------------------------------------------


#: Total executor chat dispatch attempts (1 initial + retries) before giving up
#: on a RETRYABLE (transient task-failed) outcome. A transient worker ``exit 1``
#: clears on a re-dispatch; without this a single blip killed the whole run.
_EXECUTOR_CHAT_ATTEMPTS = 3
#: Linear backoff between retries (``_RETRY_BACKOFF_S * attempt`` seconds).
_EXECUTOR_CHAT_RETRY_BACKOFF_S = 1.0


@dataclass(slots=True)
class ExecutorAdapter:
    """Worker / CLI adapter for ``provider="executor"`` accounts.

    Each :meth:`chat` invocation creates ONE ``executor_tasks`` row
    (``run_id=None`` — chat is detached from any ExecutionRun, no artifact
    capture), XADDs it onto the bound worker's stream, and awaits the
    worker's POSTed result. The worker runs the appropriate CLI
    (claude_code / codex / opencode) ``--print`` mode subprocess against
    the rendered prompt, and the CLI's stdout text becomes
    :attr:`ChatResponse.content`.

    Why ``run_id=None``: a chat turn is not a code-running task — the
    worker still creates and tears down its per-task local dir, but any
    files the CLI happens to write are discarded. The BSVibe agent loop
    owns the workflow + tools; the executor is just a transport for the
    LLM call (founder's Pro subscription used through the host CLI cred).

    Tool calling is NOT supported through this transport (the CLI's
    ``--print`` mode does not surface OpenAI-style tool_calls in its
    stream-json output). A non-empty ``tools`` argument raises
    :class:`NotImplementedError` so the caller sees the mismatch instead
    of silently dropping the tools. Every static caller in
    :mod:`backend.dispatch.caller_registry` declares ``required_methods =
    {"chat"}`` only, so this is invariant-aligned.
    """

    account: ModelAccount
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    session: AsyncSession
    settings: Settings
    redis: Any = None
    # Lift E9 — per-caller chat timeout. ``None`` falls back to
    # ``settings.executor_task_timeout_s`` (the legacy 1800 s default,
    # right for ``workflow.agent_loop.act`` but wrong for chat-shaped
    # callers like ``knowledge.ingest`` that finish in 10-60 s when
    # the worker is healthy).
    timeout_s: float | None = None
    # Yield-back on saturation — set from :attr:`CallerSpec.yield_on_saturation`
    # by :func:`adapter_for`. When ``True`` and all live workers are at
    # capacity, :func:`_await_worker_with_capacity` raises
    # :class:`ExecutorCapacitySaturated` IMMEDIATELY instead of blocking up to
    # ``settings.executor_capacity_wait_max_s``. Run-drive callers (frame /
    # agent-loop) set it True — the AgentWorker re-polls their run, so leaving
    # it open beats holding the shared worker slot. Batch callers keep it
    # ``False`` and retain the bounded wait.
    yield_on_saturation: bool = False
    # Lift E19 — optional ``session_factory`` so each ``chat`` call can
    # open its OWN ``AsyncSession`` for the dispatch lifecycle
    # (create_task → dispatch_task → commit → await_completion). When
    # set, the bound ``self.session`` is NOT used by the chat path; it
    # stays on the dataclass only as the legacy-caller fallback below.
    #
    # Why this exists: Lift E18 parallelised :meth:`IngestCompiler.compile_batch`
    # via ``asyncio.gather`` (parallelism=3 default). When several parallel
    # chunks all called this adapter with the SAME ``self.session`` (the one
    # the bootstrap / settle runtime opened up-stream), two concurrent
    # ``session.flush()`` calls hit SQLAlchemy's "Session is already flushing"
    # guard (``InvalidRequestError``). Live dogfood @ 01:40:37 saw chunks
    # 0/2/3 of 591 raise the guard at the same microsecond. Per-call
    # ``session_factory`` removes the shared-state hazard at its root: every
    # parallel branch owns its own write transaction.
    session_factory: async_sessionmaker[AsyncSession] | None = None
    # Lift E31 — when the caller is an ``agent_loop.act`` invocation the
    # executor task IS part of an ExecutionRun. Threading the run id onto
    # ``dispatch.create_task`` is what flips ``record_result``'s file-
    # persist guard (``files and task.run_id is not None``) so the files
    # the coding agent wrote inside its sandbox land under the run's
    # vault path as real ``artifact_refs``. Chat-shaped callers (frame /
    # judge / knowledge.ingest) leave this ``None`` — their tasks are
    # detached from any run and capturing files would be noise.
    run_id: uuid.UUID | None = None
    # Lift E32 — when set, the dispatched task tells the worker to
    # shallow-clone this git URL into the per-task workspace before
    # invoking the executor. Without it the worker hands the coding agent
    # an empty ``tempfile.mkdtemp()`` and the agent has nothing to read or
    # edit (the E31 dogfood symptom). Chat-shaped callers leave NULL.
    repo_url: str | None = None
    supported_methods: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        # Lift E30 — when the caller passes tools the loop expects to drive a
        # tool-use cycle (BSVibe = orchestrator, LLM = planner emitting
        # structured tool_calls). Coding agents (opencode / codex /
        # claude_code) work the OTHER way around: the agent acts
        # autonomously inside its sandbox with its OWN tools (bash, git, gh,
        # file edit, pytest, …). The single piece BSVibe genuinely needs
        # from them is the verification contract that gates ``verified``.
        #
        # Impedance-match the two patterns: format the BSVibe tools as a
        # short natural-language reference block in ``system`` so the agent
        # knows the contract surface, single-shot the chat call, then parse
        # the agent's final text for a ``<verification-contract>{...}</…>``
        # block and synthesize a virtual ``declare_verification`` tool call.
        # ``_drive_loop`` then registers the contract and runs verification
        # exactly as it does for LiteLLM-backed accounts.

        if self.redis is None:
            raise ExecutorAdapterUnavailable(
                "ExecutorAdapter requires a Redis client to dispatch onto the "
                "worker stream — no redis was wired into the resolver"
            )

        extra = self.account.extra_params or {}
        executor_type = str(extra.get("executor_type") or "")

        # T2 — ``tools`` means the agent will ACT. On this transport that has to happen
        # through BSVibe's tools, server-side (the executor is the user's LLM client, not an
        # execution environment). An executor CLI we cannot hand those tools to cannot honour
        # the contract, so it is refused — never quietly run in the old shape, where it acts
        # with its OWN tools in a temp dir the worker scrapes back. That shape is what let an
        # agent invent a codebase in an empty dir and ship it (parity audit #1).
        #
        # Chat turns (no tools) are unaffected: every executor still serves those.
        if tools and executor_type and not supports_remote_tools(executor_type):
            raise ExecutorAdapterUnavailable(
                f"executor {executor_type!r} cannot use BSVibe's tools — agentic work cannot "
                "run on it. Route this work to a claude_code executor or a LiteLLM account."
            )
        if not executor_type:
            raise ExecutorAdapterUnavailable(
                f"ExecutorAdapter account {self.model_account_id} has no "
                "executor_type in extra_params — the executor model row is malformed"
            )
        pinned_raw = extra.get("worker_id")
        pinned_worker_id = _parse_uuid_or_none(pinned_raw)

        # Fold EVERY system-role message into the system slot. ``ResolverLoopLlm``
        # lifts only the first one; a caller that grounds an answer sends several
        # (the product's state, the retrieved knowledge). LiteLLM passes them all to
        # the provider — dropping them here (which ``_render_prompt`` used to do)
        # meant the executor handed the model the bare question and it answered
        # "제공된 지식이 없습니다" (prod, 2026-07-13). Same input, either transport.
        system = "\n\n".join(
            part for part in [system, *(_system_texts(messages))] if part and part.strip()
        )
        prompt = _render_prompt(messages)

        # Localize the system prompt the SAME way the LiteLLM adapter does, so
        # executor-generated prose (verify demonstration, decision questions,
        # note bodies) follows the workspace language too — one shared helper,
        # identical behaviour across transports.
        system = _system_with_output_language(system)

        # T3 — the E30 "impedance match" is gone. It rendered BSVibe's tool schemas into the
        # system prompt as PROSE and told the agent to "use your OWN tools — Read/Edit/Write/
        # Bash", then parsed a ``<verification-contract>`` block back out of the reply text and
        # forged a ``declare_verification`` tool call from it. The agent now HAS BSVibe's tools,
        # for real, over MCP — so it declares its contract by calling the tool.
        #
        # A chat turn (tools None) still gets the completion directive: a coding CLI must be
        # told to answer as a plain completion endpoint.
        effective_system = system if tools else _augment_system_for_executor_chat(system)

        # Lift E19 — when wired with a ``session_factory`` the entire
        # dispatch lifecycle (create_task → dispatch_task → commit →
        # await_completion) runs on a FRESH ``AsyncSession``. Parallel
        # chat calls on the same adapter no longer race on a shared
        # session's flush state. The legacy ``self.session`` is the
        # fallback path so the optional plumbing is fully additive.
        async def _dispatch_once() -> ChatResponse:
            if self.session_factory is not None:
                # Lift E19 telemetry — visible signal that the per-call
                # session path is active. Helps verify wiring in prod.
                logger.debug(
                    "executor_adapter_chat_per_call_session",
                    workspace_id=str(self.workspace_id),
                    account_id=str(self.model_account_id),
                )
                async with self.session_factory() as call_session:
                    return await self._chat_with_session(
                        session=call_session,
                        executor_type=executor_type,
                        pinned_worker_id=pinned_worker_id,
                        system=effective_system,
                        prompt=prompt,
                        agentic=bool(tools),
                    )

            # Legacy path — caller hasn't migrated; use the bound session.
            # ⚠️ Concurrent ``chat`` calls on the same adapter through this
            # path race on ``session.flush()`` (the E18 hazard). Callers that
            # fan out (``IngestCompiler.compile_batch``) MUST wire
            # ``session_factory`` instead.
            logger.warning(
                "executor_adapter_chat_bound_session",
                workspace_id=str(self.workspace_id),
                account_id=str(self.model_account_id),
                note=(
                    "no session_factory wired — using bound session. "
                    "Concurrent chat calls will race on session.flush() (E18). "
                    "Lift E19: thread async_sessionmaker through the resolver."
                ),
            )
            return await self._chat_with_session(
                session=self.session,
                executor_type=executor_type,
                pinned_worker_id=pinned_worker_id,
                system=effective_system,
                prompt=prompt,
                agentic=bool(tools),
            )

        # Re-dispatch a RETRYABLE (transient task-failed) outcome a bounded
        # number of times — a worker ``exit 1`` blip clears on the next call
        # (live dogfood). Each retry creates a fresh task (and, on the E19 path,
        # a fresh session). Non-retryable modes (no redis / no executor_type /
        # timeout / no capacity) raise on the first attempt, unchanged.
        for attempt in range(1, _EXECUTOR_CHAT_ATTEMPTS + 1):
            try:
                return await _dispatch_once()
            except ExecutorAdapterUnavailable as exc:
                if not exc.retryable or attempt >= _EXECUTOR_CHAT_ATTEMPTS:
                    raise
                logger.warning(
                    "executor_adapter_chat_retry",
                    workspace_id=str(self.workspace_id),
                    account_id=str(self.model_account_id),
                    attempt=attempt,
                    max_attempts=_EXECUTOR_CHAT_ATTEMPTS,
                    error=str(exc),
                )
                await asyncio.sleep(_EXECUTOR_CHAT_RETRY_BACKOFF_S * attempt)
        # Unreachable — the loop returns or raises on the final attempt.
        raise AssertionError("executor chat retry loop exited without result")

    async def _work_tool_surface(
        self, session: AsyncSession, *, agentic: bool
    ) -> dict[str, Any] | None:
        """Mint the run-scoped token and build the MCP surface for an agentic task.

        ``None`` for a chat turn (no tools, nothing to reach). A run-less agentic task cannot
        be scoped to anything, so it is refused rather than handed a workspace-wide token.
        """
        if not agentic:
            return None
        if self.run_id is None:
            raise ExecutorAdapterUnavailable(
                "an agentic executor task must belong to a run — there is nothing to scope its "
                "tools to"
            )
        from backend.identity.oauth_service import issue_run_task_token  # noqa: PLC0415

        token = await issue_run_task_token(
            session,
            run_id=self.run_id,
            workspace_id=self.workspace_id,
            user_id=await _founder_of(session, self.workspace_id),
            issuer=self.settings.oauth_issuer,
        )
        return build_work_tool_dispatch(token=token, issuer=self.settings.oauth_issuer)

    async def _chat_with_session(
        self,
        *,
        session: AsyncSession,
        executor_type: str,
        pinned_worker_id: uuid.UUID | None,
        system: str,
        prompt: str,
        agentic: bool,
    ) -> ChatResponse:
        """Run one chat-task dispatch lifecycle against a single session.

        Lift E19 — extracted so the same dispatch-side logic runs whether
        the caller wired a ``session_factory`` (each chat = fresh session)
        or the legacy bound ``self.session``. Pre-E19 this was inline.
        """
        from backend.executors import dispatch  # noqa: PLC0415

        worker = await _await_worker_with_capacity(
            session=session,
            workspace_id=self.workspace_id,
            executor_type=executor_type,
            pinned_worker_id=pinned_worker_id,
            settings=self.settings,
            account_id=self.model_account_id,
            yield_on_saturation=self.yield_on_saturation,
        )

        # Lift E21 — forward the underlying LLM model id from the
        # account so the worker can pass it to the executor's HTTP body.
        # Empty string and the legacy ``executor/<executor_type>`` placeholder
        # both mean "no override — use the CLI default" (pre-E21 shape).
        raw_model = (self.account.litellm_model or "").strip()
        model = None if not raw_model or raw_model.startswith("executor/") else raw_model

        # Create + dispatch the task. Lift E31 — when the resolver wired
        # ``self.run_id`` (agent_loop callers), thread it onto the task row
        # so the worker's captured files (B1) get persisted as the run's
        # ``artifact_refs`` via ``record_result``. Pre-E31 / chat-shaped
        # callers leave ``run_id=None`` and capture is silently skipped.
        task = await dispatch.create_task(
            session,
            workspace_id=self.workspace_id,
            executor_type=executor_type,
            prompt=prompt,
            system=system,
            workspace_dir=".",
            run_id=self.run_id,
            model=model,
            repo_url=self.repo_url,
            # Parity with LiteLLM (BSVibe's first principle): ``tools`` is what
            # tells a model it may act. No tools → a plain completion, so the
            # executor CLI must run WITHOUT its own tools too. Left agentic, it
            # reads its empty per-task dir and answers about that instead of the
            # grounding we injected (prod 2026-07-13, "현 프로젝트 상황 설명해줘").
            agentic=agentic,
        )
        # T2b-4 — an agentic turn acts through BSVibe's tools: hand the worker the MCP
        # endpoint, a token scoped to THIS run, and the exact tool names it may use. The CLI's
        # own tools are taken away, so there is no local temp dir to invent code in and nothing
        # for the worker to scrape back.
        mcp = await self._work_tool_surface(session, agentic=agentic)
        await dispatch.dispatch_task(
            self.redis, session=session, task=task, worker_id=worker.id, mcp=mcp
        )
        # Commit before awaiting — the worker reports its result on a
        # SEPARATE session over HTTP (/api/v1/workers/result), whose
        # ``record_result`` does ``session.get(ExecutorTaskRow)``. Under
        # PG READ COMMITTED an uncommitted row is invisible to the
        # worker's session, so the worker can never flip it terminal and
        # we'd block the full timeout.
        await session.commit()

        # Lift E9 — per-caller chat timeout. ``self.timeout_s`` is set
        # from :attr:`CallerSpec.default_timeout_s` at construction time
        # (see :func:`adapter_for`); ``None`` keeps the legacy global
        # ``settings.executor_task_timeout_s`` for long-running coding-
        # agent callers (``workflow.agent_loop.act``, ~5-15 min).
        effective_timeout_s = (
            self.timeout_s if self.timeout_s is not None else self.settings.executor_task_timeout_s
        )
        try:
            completed = await dispatch.await_completion(
                self.redis,
                session=session,
                task_id=task.id,
                timeout_s=effective_timeout_s,
            )
        except dispatch.TaskTimeout as exc:
            # Lift E14 — signal the worker so it stops running the
            # now-abandoned subprocess. The dogfood symptom (bsvibe-app
            # big-repo bootstrap) was the backend marking 25 chunks as
            # ``failed`` while the worker's ``opencode run`` for each one
            # kept burning CPU + memory for many more minutes. The cancel
            # is best-effort — :func:`dispatch.cancel_task` swallows
            # redis hiccups since the backend has already raised on its
            # own caller. We log + raise unconditionally.
            logger.info(
                "executor_adapter_chat_timeout",
                workspace_id=str(self.workspace_id),
                account_id=str(self.model_account_id),
                worker_id=str(worker.id),
                task_id=str(task.id),
                timeout_s=effective_timeout_s,
            )
            await dispatch.cancel_task(self.redis, worker_id=worker.id, task_id=task.id)
            raise ExecutorAdapterUnavailable(
                f"executor chat task {task.id} timed out: {exc}"
            ) from exc

        if completed.status != "done":
            # Retryable: a task that came back ``failed`` is usually a transient
            # worker-side blip (CLI ``exit 1`` / momentary API or resource
            # pressure) that clears on a re-dispatch — see the chat() retry loop.
            raise ExecutorAdapterUnavailable(
                f"executor chat task {task.id} failed: "
                f"{completed.error_message or 'no error message'}",
                retryable=True,
            )

        logger.info(
            "executor_adapter_chat_complete",
            workspace_id=str(self.workspace_id),
            account_id=str(self.model_account_id),
            worker_id=str(worker.id),
            task_id=str(task.id),
            executor_type=executor_type,
            output_chars=len(completed.output or ""),
        )
        # T3 — no synthesized tool call, and nothing scraped to surface. The agent's real tool
        # calls went to the MCP work tools (server-side); the loop reads what it declared and
        # wrote from the run's own state. An executor turn returns plain text, exactly like a
        # LiteLLM completion does.
        return ChatResponse(content=completed.output or "")


#: Directive that makes a coding-agent executor behave like a raw LLM completion
#: for a chat-shaped (``tools=None``) call — see :func:`_augment_system_for_executor_chat`.
_EXECUTOR_COMPLETION_DIRECTIVE = (
    "\n\n---\n"
    "You are being used as a TEXT-COMPLETION endpoint, NOT an interactive agent. "
    "Do exactly what the instruction above asks, in ONE reply, and output ONLY "
    "the requested result — no preamble, no explanation, no clarifying questions, "
    "no tool use, no markdown code fences. If the instruction asks for a JSON "
    "object, your ENTIRE reply must be that single JSON object and nothing else."
)


def _augment_system_for_executor_chat(system: str) -> str:
    """Make a coding-agent executor behave like a raw LLM for a pure completion
    call (``tools=None`` — framing, judging, note synthesis, decision questions).

    claude_code / codex / opencode are AGENTIC by default: given a "return a JSON
    object" prompt, ``claude -p`` may reply with preamble, a clarifying question,
    or an agentic transcript — none of which the caller's parser can read, so the
    call degrades exactly like a LiteLLM refusal (live: ``frame_stage_llm_unparseable``
    → no ``summary_title`` → the report title falls back to the raw imperative
    Direction). Appending a completion directive keeps ``ExecutorAdapter.chat``
    behaviourally identical to ``LiteLLMAdapter.chat`` for chat-shaped callers —
    one ``chat`` contract, same clean output, regardless of transport."""
    return (system.rstrip() if system else "") + _EXECUTOR_COMPLETION_DIRECTIVE


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
    redis: Any = None,
    timeout_s: float | None = None,
    yield_on_saturation: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    run_id: uuid.UUID | None = None,
    repo_url: str | None = None,
) -> ModelAccountAdapter:
    """Pick the right :class:`ModelAccountAdapter` for an account.

    Executor accounts (per :func:`is_executor_account`) →
    :class:`ExecutorAdapter`. Anything else → :class:`LiteLLMAdapter`.

    ``api_key`` is the already-decrypted key the caller pulled from
    :meth:`backend.router.accounts.service.ModelAccountService.reveal_api_key`;
    we never decrypt inside the adapter so credentials never leak
    through logs emitted from this module.

    ``redis`` is the Redis client used by the ExecutorAdapter to dispatch
    onto the worker stream. May be ``None`` when no executor account is
    expected (a workspace with only LiteLLM accounts); resolving an
    executor account with no redis later raises
    :class:`ExecutorAdapterUnavailable`.

    ``timeout_s`` (Lift E9) — per-caller chat timeout in seconds, taken
    from :attr:`CallerSpec.default_timeout_s` by the resolver. ``None``
    falls back to ``settings.executor_task_timeout_s`` on the executor
    path and to LiteLLM's own default on the LiteLLM path. The adapter
    closes over the value at construction so :meth:`chat` does not
    re-walk the caller registry per call.

    ``yield_on_saturation`` — taken from :attr:`CallerSpec.yield_on_saturation`
    by the resolver, threaded onto the :class:`ExecutorAdapter` the SAME way as
    ``timeout_s``. ``True`` for run-drive callers whose run the AgentWorker
    re-polls: on saturation the adapter raises :class:`ExecutorCapacitySaturated`
    immediately (yield-back) rather than blocking the shared worker. Ignored on
    the LiteLLM path (no worker capacity to saturate).

    ``session_factory`` (Lift E19) — optional ``async_sessionmaker`` the
    :class:`ExecutorAdapter` uses to open a fresh ``AsyncSession`` per
    ``chat`` call. When set, parallel chat calls (the
    :meth:`IngestCompiler.compile_batch` ``asyncio.gather`` fan-out) no
    longer share + race on the bound session's flush state. ``None``
    keeps the legacy path; the LiteLLM adapter never needs it.
    """
    if is_executor_account(account):
        return ExecutorAdapter(
            account=account,
            workspace_id=account.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
            session=session,
            settings=settings,
            redis=redis,
            timeout_s=timeout_s,
            # Yield-back on saturation for run-drive callers — threaded from
            # ``CallerSpec.yield_on_saturation`` alongside ``timeout_s``.
            yield_on_saturation=yield_on_saturation,
            # Lift E19 — when the resolver was wired with an
            # ``async_sessionmaker`` the adapter opens a fresh session
            # per ``chat`` call so parallel chunks don't race on
            # ``session.flush()``. ``None`` keeps the legacy path.
            session_factory=session_factory,
            # Lift E31 — agent_loop callers thread the ExecutionRun's id
            # so files captured by the worker land under the run.
            run_id=run_id,
            # Lift E32 — agent_loop callers thread the product's repo URL
            # so the worker clones it into the per-task workspace.
            repo_url=repo_url,
        )
    return LiteLLMAdapter(
        account=account,
        api_key=api_key,
        llm=llm or LlmClient(),
        workspace_id=account.workspace_id,
        account_id=account.account_id,
        model_account_id=account.id,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _await_worker_with_capacity(
    *,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    executor_type: str,
    pinned_worker_id: uuid.UUID | None,
    settings: Settings,
    account_id: uuid.UUID,
    yield_on_saturation: bool = False,
) -> Any:
    """Lift E16 — block until a worker with free capacity is available.

    Loops :func:`backend.executors.dispatch.find_available_worker` with the
    workspace's per-worker parallel cap; sleeps
    ``settings.executor_capacity_wait_poll_s`` between checks; gives up
    after ``settings.executor_capacity_wait_max_s`` with a distinct
    :class:`ExecutorAdapterUnavailable` so the caller can tell saturation
    apart from misconfig.

    Why a separate helper: the retry math + bounded-wait log boundaries
    don't belong inline in :meth:`ExecutorAdapter.chat` (already a
    multi-step orchestration). The helper closes over the dispatch
    invariants (pin honoured even when saturated lives in
    :func:`find_available_worker`; we just propagate the result).
    """
    from backend.executors import dispatch  # noqa: PLC0415

    deadline = asyncio.get_event_loop().time() + settings.executor_capacity_wait_max_s
    poll_interval = max(settings.executor_capacity_wait_poll_s, 0.0)
    attempt = 0
    while True:
        attempt += 1
        worker = await dispatch.find_available_worker(
            session,
            workspace_id=workspace_id,
            executor_type=executor_type,
            pinned_worker_id=pinned_worker_id,
            max_parallel_per_worker=settings.max_parallel_tasks_per_worker,
        )
        if worker is not None:
            return worker
        # No available worker. Two very different conditions produce this None:
        #   (A) live workers exist but are all at capacity → waiting is
        #       legitimate; a slot may free up. Fall through to the bounded wait.
        #   (B) NO online, capability-matching, fresh-heartbeat worker exists at
        #       all (a dead/offline pin, none live) → waiting is FUTILE; no
        #       amount of waiting conjures capacity. Fail fast so the shared
        #       AgentWorker is not wedged for up to executor_capacity_wait_max_s
        #       (30 min default) starving every other workspace's runs.
        # Checked on EVERY iteration (a worker can go offline mid-wait), before
        # the deadline/sleep logic. This is the slow path (worker unavailable),
        # so the extra query cost is irrelevant.
        if not await dispatch.has_live_worker(
            session, workspace_id=workspace_id, executor_type=executor_type
        ):
            logger.info(
                "executor_adapter_no_live_worker",
                workspace_id=str(workspace_id),
                account_id=str(account_id),
                executor_type=executor_type,
                attempt=attempt,
            )
            raise ExecutorAdapterUnavailable(
                f"no live {executor_type!r} worker in workspace {workspace_id} "
                f"— failing fast instead of waiting {settings.executor_capacity_wait_max_s}s"
            )
        # Live workers exist but all are at capacity (genuine saturation).
        #   * Run-drive caller (yield_on_saturation) → do NOT block: the shared
        #     AgentWorker re-polls this OPEN run, so raising ExecutorCapacitySaturated
        #     now (yield-back) lets the run retry next poll while the worker
        #     serves other workspaces. Blocking up to executor_capacity_wait_max_s
        #     (30 min) would hold the worker slot + the run's DB lock and starve
        #     EVERY other workspace's runs.
        #   * Batch caller (default) → fall through to the bounded wait: an
        #     ingest/canonicalization fan-out (asyncio.gather) cannot yield to a
        #     poll loop, so waiting for a slot is its only legitimate option.
        if yield_on_saturation:
            logger.info(
                "executor_adapter_yield_saturated",
                workspace_id=str(workspace_id),
                account_id=str(account_id),
                executor_type=executor_type,
                attempt=attempt,
            )
            raise ExecutorCapacitySaturated(
                f"live {executor_type!r} worker(s) in workspace {workspace_id} are all "
                "at capacity — yielding back so the AgentWorker re-polls this run "
                "instead of blocking the shared worker"
            )
        now = asyncio.get_event_loop().time()
        elapsed = settings.executor_capacity_wait_max_s - (deadline - now)
        if now >= deadline:
            logger.info(
                "executor_adapter_capacity_wait_exhausted",
                workspace_id=str(workspace_id),
                account_id=str(account_id),
                executor_type=executor_type,
                attempts=attempt,
                elapsed_s=round(elapsed, 3),
                max_wait_s=settings.executor_capacity_wait_max_s,
            )
            raise ExecutorAdapterUnavailable(
                f"no worker capacity within {settings.executor_capacity_wait_max_s}s "
                f"for executor {executor_type!r} in workspace {workspace_id} "
                f"— workspace appears stuck or under-provisioned"
            )
        logger.info(
            "executor_adapter_awaiting_capacity",
            workspace_id=str(workspace_id),
            account_id=str(account_id),
            executor_type=executor_type,
            attempt=attempt,
            elapsed_s=round(elapsed, 3),
        )
        # Cap sleep at the remaining budget so we don't overshoot the deadline.
        await asyncio.sleep(min(poll_interval, deadline - now))


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


def _render_prompt(messages: list[ChatMessage]) -> str:
    """Render an OpenAI-style ``messages`` list as a single CLI prompt.

    The CLI subprocess takes ONE prompt over stdin (``claude --print``,
    ``codex -p``, …). We render the conversation as a simple
    role-tagged transcript so the CLI sees the same turn history a chat
    completion endpoint would. The ``system`` slot is already shipped
    separately as ``--append-system-prompt`` (the executor streamer's
    ``context["system"]``) and is NOT re-rendered here.

    Tool-call messages (``role == "tool"``) are rendered as
    ``[tool:<name>] <content>`` for completeness, though :meth:`ExecutorAdapter.chat`
    rejects ``tools=[...]`` upstream so this branch only fires for a
    pre-existing tool-result message in the history.
    """
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            # OpenAI's content-parts shape — concatenate the text parts.
            text_parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            content = "".join(text_parts)
        if content is None:
            content = ""
        if role == "tool":
            tool_name = str(message.get("name") or "tool")
            parts.append(f"[tool:{tool_name}] {content}")
        elif role == "system":
            # Not dropped — :meth:`ExecutorAdapter.chat` has already folded every
            # system message into the ``system`` slot the CLI is given. Skipping it
            # here just avoids rendering it twice.
            continue
        else:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts).strip()


def _system_texts(messages: list[ChatMessage]) -> list[str]:
    """Every system-role message's text, in order (see :meth:`ExecutorAdapter.chat`)."""
    texts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if content:
            texts.append(str(content))
    return texts


def _parse_uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None
