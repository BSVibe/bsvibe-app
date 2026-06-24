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
  ``codex -p`` / ``opencode -p`` exactly as it does for the legacy
  :class:`~backend.executors.coordinator.ExecutorOrchestrator` full-run
  path — same ``execute(prompt, context)`` streamers (claude_code.py /
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
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import is_executor_account
from backend.router.llm_client import LlmClient, LlmResponse

logger = structlog.get_logger(__name__)

__all__ = [
    "EXECUTOR_DECLARE_VERIFICATION_ID",
    "ChatMessage",
    "ChatResponse",
    "ChatToolCall",
    "ExecutorAdapter",
    "ExecutorAdapterUnavailable",
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
    # Relative paths the worker captured for an executor chat that was bound to
    # a run (the coding agent's edits in its per-task clone). Empty for the
    # LiteLLM path and for run-less chat-shaped executor calls. The agent loop
    # merges these into its ``written_paths`` so the verified Deliverable's
    # ``artifact_refs`` reflect what the agent actually changed.
    artifact_refs: tuple[str, ...] = ()


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


class ExecutorAdapterUnavailable(RuntimeError):
    """Raised by :meth:`ExecutorAdapter.chat` when the chat cannot dispatch.

    Three distinct failure modes share one exception type because every
    call site treats them the same way (surface to the user / write a
    Decision, never a silent fallback):

    * No Redis client wired into the resolver (the worker stream needs it).
    * The executor account's ``extra_params`` is missing
      ``executor_type``.
    * No online worker carries the requested capability (Decision in the
      legacy full-run path; here we raise so the chat call site sees it).
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
        # Thread the workspace OUTPUT language into the prose-generating chat
        # path (frame / ingest / judge / agent-loop questions all resolve a
        # LiteLLM adapter): empty for English, else a "write prose in <lang>"
        # suffix. Set on the contextvar by the resolver from workspaces.language.
        from backend.identity.output_language import language_directive  # noqa: PLC0415

        full_messages: list[ChatMessage] = [
            {"role": "system", "content": system + language_directive()}
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
        if not executor_type:
            raise ExecutorAdapterUnavailable(
                f"ExecutorAdapter account {self.model_account_id} has no "
                "executor_type in extra_params — the executor model row is malformed"
            )
        pinned_raw = extra.get("worker_id")
        pinned_worker_id = _parse_uuid_or_none(pinned_raw)

        prompt = _render_prompt(messages)

        # Lift E30 — when ``tools`` is set, give the agent a short reference
        # of BSVibe's expected verification contract so it can declare one
        # in its final text. The agent does its actual work using its own
        # native tools (bash, git, gh, file edit, pytest); BSVibe's tool
        # registry is here only as a verification-contract template.
        effective_system = _augment_system_for_executor_tools(system, tools) if tools else system

        # Lift E19 — when wired with a ``session_factory`` the entire
        # dispatch lifecycle (create_task → dispatch_task → commit →
        # await_completion) runs on a FRESH ``AsyncSession``. Parallel
        # chat calls on the same adapter no longer race on a shared
        # session's flush state. The legacy ``self.session`` is the
        # fallback path so the optional plumbing is fully additive.
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
        )

    async def _chat_with_session(
        self,
        *,
        session: AsyncSession,
        executor_type: str,
        pinned_worker_id: uuid.UUID | None,
        system: str,
        prompt: str,
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
        )
        await dispatch.dispatch_task(self.redis, session=session, task=task, worker_id=worker.id)
        # Commit before awaiting — the worker reports its result on a
        # SEPARATE session over HTTP (/api/v1/workers/result), whose
        # ``record_result`` does ``session.get(ExecutorTaskRow)``. Under
        # PG READ COMMITTED an uncommitted row is invisible to the
        # worker's session, so the worker can never flip it terminal and
        # we'd block the full timeout. Same invariant the
        # :class:`ExecutorOrchestrator` carries.
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
            raise ExecutorAdapterUnavailable(
                f"executor chat task {task.id} failed: "
                f"{completed.error_message or 'no error message'}"
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
        # Lift E30 — when the agent emitted a verification contract block,
        # synthesize a virtual ``declare_verification`` tool call so the
        # downstream loop registers the contract and runs verification
        # exactly as it does for LiteLLM-backed accounts. Absent a contract
        # block the return shape is the legacy one (``tool_calls=()``) and
        # the loop nudges the agent on the next cycle.
        synthesized = _synthesize_executor_tool_calls(completed.output or "")
        # Surface the worker-captured files (B1 — persisted on the task row when
        # the chat was bound to a run) so the loop can record them as the
        # verified Deliverable's artifact_refs. The agent edits files with its
        # OWN sandbox tools, never the loop's file_write, so without this the
        # deliverable's artifact_refs comes out empty for every executor run.
        captured = tuple(completed.artifact_refs or ())
        return ChatResponse(
            content=completed.output or "",
            tool_calls=synthesized,
            artifact_refs=captured,
        )


# ---------------------------------------------------------------------------
# Lift E30 — executor-side helpers
# ---------------------------------------------------------------------------

_E30_TOOL_GUIDE_HEADER = (
    "## BSVibe coding-agent contract — MANDATORY (Lift E30 / E34 / E37)\n"
    "\n"
    "⚠ REQUIRED — your final message MUST end with this exact block:\n"
    "\n"
    "<verification-contract>\n"
    '{"checks": [{"kind": "command", "command": "<shell command BSVibe should re-run to verify>"}]}\n'
    "</verification-contract>\n"
    "\n"
    "Without this block BSVibe ROUTES THE RUN TO HUMAN REVIEW and your "
    "edits WILL NOT SHIP as a PR. This is the gate to ``verified`` — no "
    "block, no ship. A trivial pass-through is acceptable when no real "
    "command makes sense, e.g. "
    '``{"checks": [{"kind": "command", "command": "test -f <one-of-the-files-you-changed>"}]}``\n'
    "— but the block ITSELF is non-negotiable.\n"
    "\n"
    "---\n"
    "\n"
    "You are a coding agent running inside a sandbox that ALREADY has the "
    "product repo checked out at your current working directory (Lift E32). "
    "Use your OWN tools — Read / Edit / Write / Bash — to read the files, "
    "make the actual edits, and run tests. BSVibe does NOT call tools on "
    "your behalf; the agent_loop will not give you another turn unless you "
    "explicitly fail to declare the verification contract.\n"
    "\n"
    "**Do all the work in this single response.** Concretely:\n"
    "1. Read the files referenced in the user prompt.\n"
    "2. Make the edits using your Edit / Write tools.\n"
    "3. Run the verification commands yourself (Bash tool, e.g. ``pytest`` / "
    "``ruff check``) to confirm they pass BEFORE declaring the contract.\n"
    "4. End your final message with the MANDATORY contract block shown "
    "above. (See top of this guide — the block is REQUIRED; a textual "
    "summary alone is REJECTED.)\n"
    "\n"
    "Each check is a shell command whose exit-code-0 means verified. After "
    "you emit the contract BSVibe re-runs every check in its own sandbox "
    "and the run lands ``verified`` if all pass — DO NOT just describe what "
    "to do, DO IT. Planning without editing is the failure mode the loop "
    "punishes by re-prompting you until contract is declared.\n"
    "\n"
    "Capture (Lift E33 + E36): BSVibe records files that ``git status`` "
    "reports as changed against your post-clone baseline AND files in any "
    "commits you made on top of ``FETCH_HEAD``. ``git checkout -b … && git "
    "commit`` is fine — both committed and working-tree edits surface.\n"
    "\n"
    "BSVibe's tool registry (FOR REFERENCE — you do NOT call these; emit "
    "the contract instead):\n"
)

_E30_CONTRACT_RE = re.compile(
    r"<verification-contract>\s*(?P<json>\{.*?\})\s*</verification-contract>",
    re.DOTALL,
)

#: Stable id stamped on the synthesized ``declare_verification`` tool call so
#: the agent loop can recognize it as the SINGLE-SHOT executor's terminal
#: declaration. Coding-agent executors (claude_code / codex / opencode) do all
#: their work in one ``--print`` turn and re-emit the ``<verification-contract>``
#: block EVERY turn, so this synthesized call appears on every turn and
#: ``tool_calls`` is never empty. The loop reaches verification only on an
#: empty-tool_calls turn (a LiteLLM model signalling "done"), which the
#: executor never returns — so the loop treats this id as terminal and verifies
#: straight away (see ``backend.workflow.application._drive_loop``).
EXECUTOR_DECLARE_VERIFICATION_ID = "e30-declare-verification"


def _augment_system_for_executor_tools(system: str, tools: list[dict[str, Any]] | None) -> str:
    """Lift E30 — append a verification-contract guide + tool reference to
    the caller's system prompt so a coding-agent executor knows what BSVibe
    needs from it.

    The agent doesn't call BSVibe tools (its OWN sandbox tools — bash, git,
    gh, file edit — do the work). The tools list is included only so the
    agent can phrase its verification contract in the same vocabulary the
    rest of the system uses.
    """
    if not tools:
        return system
    lines: list[str] = [system.rstrip() if system else "", "", _E30_TOOL_GUIDE_HEADER]
    for tool in tools:
        # OpenAI / Anthropic tool shapes share ``{"type": "function",
        # "function": {"name": ..., "description": ...}}``. Tolerate both.
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = (fn or tool).get("name") if isinstance(fn or tool, dict) else None
        desc = (fn or tool).get("description") if isinstance(fn or tool, dict) else None
        if not name:
            continue
        first = (desc or "").splitlines()[0][:200] if desc else ""
        lines.append(f"- ``{name}`` — {first}" if first else f"- ``{name}``")
    return "\n".join(lines)


def _synthesize_executor_tool_calls(output: str) -> tuple[ChatToolCall, ...]:
    """Lift E30 — extract the agent's ``<verification-contract>{…}</…>``
    block and turn it into a synthetic ``declare_verification`` tool call so
    the downstream loop registers the contract through the existing path.

    On a missing or malformed block we return ``()`` so the loop's
    no-tool-calls branch fires (it will nudge the agent on the next cycle).
    Best-effort by design — the agent's text is the source of truth, this
    layer only forwards what was declared.
    """
    if not output:
        return ()
    match = _E30_CONTRACT_RE.search(output)
    if match is None:
        return ()
    raw_json = match.group("json").strip()
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        logger.warning("executor_adapter_contract_parse_failed", raw=raw_json[:200])
        return ()
    if not isinstance(parsed, dict):
        return ()
    # The synthesized call goes through the same ``ToolRegistry`` invoker
    # as a LiteLLM-emitted ``declare_verification`` call, so the arguments
    # must be the same JSON the registry's handler accepts. The handler is
    # tolerant of unknown keys; we forward whatever the agent declared.
    return (
        ChatToolCall(
            id=EXECUTOR_DECLARE_VERIFICATION_ID,
            name="declare_verification",
            arguments_json=json.dumps(parsed),
        ),
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
    redis: Any = None,
    timeout_s: float | None = None,
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
            # Defensive: ResolverLoopLlm already strips the leading
            # system message before calling the adapter, but a caller may
            # forget. Don't render it here — the adapter ships
            # ``system`` separately via ``--append-system-prompt``.
            continue
        else:
            parts.append(f"{role}: {content}")
    return "\n\n".join(parts).strip()


def _parse_uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None
