"""RunOrchestrator — the agent compute loop.

Workflow §11.3 (agent-loop iteration: plan → act → verify → branch) +
§1.2 (verify = one assembled contract: work-declared + BSage retrieval).

This module owns the *compute* lifecycle of an :class:`ExecutionRun`:
the iterative ``plan → act → verify`` loop that drives a work LLM through
a sandboxed :class:`~backend.execution.tools.ToolRegistry`, assembles a
:class:`~backend.execution.verifier.contract.VerificationContract` from
what the work LLM declared plus (optionally) BSage canonical retrieval,
runs the contract's command checks in the sandbox + judge checks via an
LLM, and lands a verdict.

:class:`~backend.orchestrator.agent_runner.AgentRunner` owns the
*transactional* lifecycle (the ``open → running → review_ready`` status
flow) and delegates the compute to this module — exactly as its docstring
already intends.

Terminal loop outcomes (Workflow §1 ε / §6 — there is no ``abandoned``):

* ``verified`` — work complete + contract passed.
* ``needs_decision`` — the loop is stuck or the LLM asked the founder a
  question; a :class:`~backend.execution.db.Decision` row is created and
  the run pauses (not a DB-terminal — resolution re-enters the loop).
* ``system_error`` — the process/infra failed (sandbox unavailable, an
  unexpected exception). Rare.

The loop depends on ONE LLM seam — the :class:`LoopLlm` Protocol — so the
caller can inject the production gateway adapter
(:class:`~backend.execution.loop_llm.GatewayLoopLlm`) or a deterministic
test stub. (Per the ``bsvibe-llm-wrapper-not-raw-litellm`` rule: one
Protocol, never a Union of concretes.)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.execution.connector_actions import (
    ConnectorActionProvider,
    ConnectorActionTool,
    loop_tool_name,
)
from backend.execution.db import (
    Decision,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.tools import ToolDefinition, ToolError, ToolRegistry
from backend.execution.verified_deliverable import write_verified_deliverable
from backend.execution.verifier.contract import (
    VerificationContract,
)
from backend.execution.verifier.service import VerificationService
from backend.skills.loader import SkillLoader
from backend.skills.tool_binding import INVOKE_SKILL_NAME, register_invoke_skill
from backend.supervisor.sandbox import SandboxManager, SandboxSession

logger = structlog.get_logger(__name__)

LoopOutcome = Literal["verified", "needs_decision", "system_error"]

# The per-command verify timeout, the judge file-context cap, and the judge
# verdict parser now live with the shared VerificationService
# (``backend.execution.verifier.service``) — the canonical home shared by both
# the native loop and the executor orchestrator.
MAX_NO_WORK_NUDGES = 2


@dataclass(frozen=True)
class LoopToolCall:
    """One tool call the work LLM emitted in a turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LoopTurn:
    """The work LLM's response for one plan turn — free text plus any
    tool calls. ``tool_calls`` empty means the model produced only prose
    (a signal that it believes the step is done)."""

    content: str
    tool_calls: tuple[LoopToolCall, ...] = ()


@runtime_checkable
class LoopLlm(Protocol):
    """The single LLM dispatch seam the loop depends on.

    ``tools`` is the OpenAI-style tool schema slice; ``None`` means a
    plain completion (used for the LLM-judge verify call). Returns a
    :class:`LoopTurn`."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn: ...


@runtime_checkable
class CanonRetriever(Protocol):
    """Read-only BSage retrieval seam (Workflow §1.2). Given the signals
    of the change (changed paths + the work summary), returns canonical
    pattern statements to fold into the verify contract as judge criteria.
    """

    async def retrieve_for_signals(self, signals: str) -> list[str]: ...


class _RetrieverSearcher:
    """Adapt a :class:`CanonRetriever` to the skill runner's :class:`Searcher`.

    The skill runner primes a skill's system prompt via ``search(query, *,
    top_k, max_chars) -> str``; the retriever speaks ``retrieve_for_signals
    (signals) -> list[str]``. This thin adapter joins the canonical statements
    into the formatted-string shape the runner expects, capped at ``max_chars``,
    and degrades to an empty string when there is no knowledge (never raises —
    matching the retriever's own graceful-empty contract)."""

    def __init__(self, retriever: CanonRetriever) -> None:
        self._retriever = retriever

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        max_chars: int = 50_000,
    ) -> str:
        try:
            statements = await self._retriever.retrieve_for_signals(query)
        except Exception:  # noqa: BLE001 — priming must never crash a skill run
            logger.warning("skill_searcher_retrieve_failed", exc_info=True)
            return ""
        cleaned = [s.strip() for s in statements if s and s.strip()][:top_k]
        if not cleaned:
            return ""
        return "\n".join(f"- {s}" for s in cleaned)[:max_chars]


@dataclass
class LoopResult:
    """Outcome of one :meth:`RunOrchestrator.run` invocation."""

    outcome: LoopOutcome
    run_id: uuid.UUID
    work_step_id: uuid.UUID | None = None
    run_attempt_id: uuid.UUID | None = None
    verification_result_id: uuid.UUID | None = None
    decision_id: uuid.UUID | None = None
    written_paths: list[str] = field(default_factory=list)
    summary: str = ""


@runtime_checkable
class RunCompute(Protocol):
    """The single compute seam :class:`~backend.orchestrator.agent_runner.AgentRunner`
    drives.

    Both the native :class:`RunOrchestrator` (api-llm path) and the
    :class:`~backend.executors.orchestrator.ExecutorOrchestrator` (CLI-worker
    path, Lift 5b) satisfy it structurally, so the worker-runtime factory can
    return either without the runner depending on a Union of concretes (per the
    ``bsvibe-llm-wrapper-not-raw-litellm`` rule: one Protocol, never a Union).
    """

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult: ...


# Tools the work LLM may use during the loop. ``ask_user_question`` is a
# loop-owned pseudo-tool (not in the shared ToolRegistry) handled inline.
WORK_TOOLS: tuple[str, ...] = (
    "file_read",
    "file_list",
    "file_write",
    "file_edit",
    "shell_exec",
    "declare_verification",
)

# B5a — knowledge_search is a read-only tool the work LLM may call mid-run to
# consult the workspace's settled canonical knowledge. Backed by the SAME
# :class:`CanonRetriever` the verifier folds in (B3). ``invoke_skill`` is
# registered separately via :func:`register_invoke_skill`. Both are only
# surfaced when the orchestrator was given a workspace ``skill_loader`` (the
# production worker factory always threads one in; legacy/test callers that
# omit it keep the original 6-tool set).
KNOWLEDGE_SEARCH_NAME = "knowledge_search"
_KNOWLEDGE_SEARCH_MAX_RESULTS = 5

ASK_USER_QUESTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user_question",
        "description": (
            "Pause the run and ask the founder a blocking question when you "
            "genuinely cannot proceed without a human decision. This creates a "
            "Decision and stops the loop until it is resolved — use it only when "
            "no tool call can unblock you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The blocking question for the founder.",
                },
                "context": {
                    "type": "string",
                    "description": "Why you are blocked / what you have tried.",
                },
            },
            "required": ["question"],
        },
    },
}

_SYSTEM_PROMPT = (
    "You are an autonomous engineer working inside a sandboxed workspace. "
    "Use the tools to inspect and change files. Before writing code, call "
    "declare_verification to commit to how the work will be checked (prefer a "
    "command check that runs the real test/lint, scoped to the files you "
    "changed). When the step is complete, stop calling tools and reply with a "
    "short plain-text summary — that triggers verification. If you are blocked "
    "on a decision only the founder can make, call ask_user_question."
)


class RunOrchestrator:
    """Drives the plan → act → verify → iterate loop for one run."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        llm: LoopLlm,
        sandbox_manager: SandboxManager,
        max_cycles: int | None = None,
        retriever: CanonRetriever | None = None,
        skill_loader: SkillLoader | None = None,
        connector_actions: ConnectorActionProvider | None = None,
        redis_client: Any = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._sandbox_manager = sandbox_manager
        self._settings = settings or get_settings()
        # B5b — the run's connector-action provider. When set, the loop surfaces
        # the workspace's available ``mcp_exposed`` connector actions (github
        # open_pr, notion create_page, …) as tools, gated by DangerAnalyzer +
        # workspace safe_mode. ``None`` (the default, every legacy caller/test +
        # a workspace with no connector accounts) keeps the loop free of
        # connector tools — zero behaviour change.
        self._connector_actions = connector_actions
        # B5a — the run's workspace SkillLoader. When set, the loop registers
        # the ``invoke_skill`` + ``knowledge_search`` tools so the work LLM can
        # discover skills and consult canonical knowledge mid-run. ``None`` (the
        # default, every legacy caller/test) keeps the original WORK_TOOLS set.
        self._skill_loader = skill_loader
        # Round cap: explicit override wins; otherwise the env-overridable
        # Settings knob (promoted in Round 9). Defaults tuned for local LLMs.
        self._max_cycles = (
            max_cycles if max_cycles is not None else self._settings.execution_work_round_budget
        )
        self._retriever = retriever
        # Optional — only supplied in worker_mode="redis_streams" (the worker
        # runtime threads its client through the orchestrator factory). ``None``
        # (the default, incl. every existing caller/test) keeps DB-polling
        # behaviour: the verified terminal emits no stream notification. Emission
        # is gated + soft-fail inside :func:`emit_stream_notification`.
        self._redis_client = redis_client

    # -- B5a: skill + knowledge tools -------------------------------------

    def _register_knowledge_tools(self, registry: ToolRegistry) -> list[str]:
        """Register ``invoke_skill`` + ``knowledge_search`` into ``registry``.

        Only when the orchestrator was given a workspace :class:`SkillLoader`
        (the production worker factory always threads one in). Returns the names
        added so the caller can fold them into the surfaced tool schema. A
        missing loader → no extra tools (legacy behaviour, empty list)."""
        loader = self._skill_loader
        if loader is None:
            return []
        searcher = _RetrieverSearcher(self._retriever) if self._retriever is not None else None
        # invoke_skill — runs a named workspace skill end-to-end. The skill
        # runner's completion seam routes through the SAME loop LLM (adapted to
        # its (system_prompt, user_input) shape); the optional searcher primes
        # the skill's system prompt with retrieved knowledge.
        register_invoke_skill(
            registry,
            loader=loader,
            completion_fn=self._skill_completion_fn,
            searcher=searcher,
        )
        # knowledge_search — read-only, lets the LLM consult canonical knowledge
        # mid-run. Backed by the retriever; empty/no-knowledge → empty-but-valid.
        registry.register(
            ToolDefinition(
                name=KNOWLEDGE_SEARCH_NAME,
                description=(
                    "Search this workspace's settled canonical knowledge for guidance "
                    "relevant to your task. Returns the most relevant canonical concept "
                    "statements (may be empty if the workspace has no settled knowledge "
                    "yet). Read-only — consult it before deciding how to do the work."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What you want to know — describe the task or topic.",
                        },
                    },
                    "required": ["query"],
                },
                handler=self._knowledge_search,
            )
        )
        return [INVOKE_SKILL_NAME, KNOWLEDGE_SEARCH_NAME]

    async def _skill_completion_fn(
        self,
        *,
        system_prompt: str,
        user_input: str,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> str:
        """Adapt the skill runner's ``CompletionFn`` to the loop LLM seam.

        The skill runner expects ``(system_prompt, user_input, *, model,
        allowed_tools) -> str``; the loop LLM speaks ``(messages, tools) ->
        LoopTurn``. A skill is a single plain completion (no tool loop here), so
        we send the composed system prompt + user input as messages with
        ``tools=None`` and return the response text."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]
        turn = await self._llm.complete(messages=messages, tools=None)
        return turn.content

    async def _knowledge_search(self, arguments: dict[str, Any]) -> str:
        """Handler for the ``knowledge_search`` tool — never raises into the loop.

        Returns a human-legible string of the top canonical statements for the
        query, or a valid empty result when there is no knowledge / no
        retriever / the retrieval fails."""
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "knowledge_search requires a non-empty 'query'."
        if self._retriever is None:
            return "No workspace knowledge is available."
        try:
            statements = await self._retriever.retrieve_for_signals(query)
        except Exception:  # noqa: BLE001 — read-only consult must never crash the loop
            logger.warning("knowledge_search_failed", exc_info=True)
            return "No workspace knowledge is available."
        statements = [s.strip() for s in statements if s and s.strip()][
            :_KNOWLEDGE_SEARCH_MAX_RESULTS
        ]
        if not statements:
            return f"No settled knowledge found for: {query}"
        lines = [f"Relevant workspace knowledge for '{query}':"]
        lines.extend(f"- {s}" for s in statements)
        return "\n".join(lines)

    # -- B5b: connector action tools (gated by DangerAnalyzer + safe_mode) --

    async def _register_connector_action_tools(
        self, registry: ToolRegistry, *, run: ExecutionRun, work_step: WorkStep
    ) -> list[str]:
        """Register the workspace's available connector actions into ``registry``.

        Only when the orchestrator was given a :class:`ConnectorActionProvider`
        (the production worker factory threads one in). Each tool's handler is
        bound to THIS run + work_step + the resolved workspace ``safe_mode`` so
        the danger-gate fires on call. Returns the surfaced tool names (namespaced
        ``<connector>__<action>``). No provider, or a workspace with no connector
        accounts → empty list (loop unchanged)."""
        provider = self._connector_actions
        if provider is None:
            return []
        tools = await provider.list_actions(run.workspace_id)
        if not tools:
            return []
        safe_mode = await self._resolve_safe_mode(run.workspace_id)
        names: list[str] = []
        for tool in tools:
            name = loop_tool_name(tool.connector, tool.action_name)
            registry.register(
                ToolDefinition(
                    name=name,
                    description=self._connector_action_description(tool),
                    parameters_schema=_connector_action_schema(tool),
                    handler=self._make_connector_action_handler(
                        tool, run=run, work_step=work_step, safe_mode=safe_mode
                    ),
                )
            )
            names.append(name)
        logger.info(
            "connector_action_tools_registered",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
            tools=names,
            safe_mode=safe_mode,
        )
        return names

    @staticmethod
    def _connector_action_description(tool: ConnectorActionTool) -> str:
        base = (
            f"Take the '{tool.action_name}' action on the '{tool.connector}' connector "
            "for this workspace. The connector credentials are injected automatically — "
            "supply only the action arguments."
        )
        if tool.is_dangerous:
            base += (
                " This action has external side effects; in Safe Mode it pauses for "
                "founder approval instead of running."
            )
        return base

    async def _resolve_safe_mode(self, workspace_id: uuid.UUID) -> bool:
        """The workspace ``safe_mode`` flag (default True — fail safe).

        A missing workspace row → True so the danger-gate never silently runs a
        dangerous action against an unknown workspace."""
        from backend.workspaces.db import WorkspaceRow  # noqa: PLC0415 — break import cycle

        row = await self._session.get(WorkspaceRow, workspace_id)
        if row is None:
            return True
        return bool(row.safe_mode)

    def _make_connector_action_handler(
        self,
        tool: ConnectorActionTool,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        safe_mode: bool,
    ) -> Any:
        """Build the registry handler for one connector action.

        The handler resolves + decrypts the account credentials into the action
        context, then applies the DangerAnalyzer gate: a dangerous action in
        Safe Mode does NOT execute — it creates a ``connector_action_approval``
        :class:`Decision` and returns a 'pending approval' result (status
        needs_approval). Otherwise it dispatches the action and feeds the result
        back to the loop. Never raises into the loop (failures become a readable
        tool result)."""
        provider = self._connector_actions
        assert provider is not None  # registration only happens with a provider

        async def handler(arguments: dict[str, Any]) -> str:
            if tool.is_dangerous and safe_mode:
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="connector_action_approval",
                    payload={
                        "plugin": tool.connector,
                        "action": tool.action_name,
                        "args": arguments,
                        "is_dangerous": tool.is_dangerous,
                    },
                    rationale=(
                        f"work LLM requested dangerous connector action "
                        f"{tool.connector}.{tool.action_name} while Safe Mode is on"
                    ),
                )
                logger.info(
                    "connector_action_gated_needs_approval",
                    run_id=str(run.id),
                    connector=tool.connector,
                    action=tool.action_name,
                    decision_id=str(decision.id),
                )
                return json.dumps(
                    {
                        "status": "needs_approval",
                        "connector": tool.connector,
                        "action": tool.action_name,
                        "message": (
                            "This action requires founder approval (Safe Mode). It has been "
                            "queued as a pending decision and was NOT executed. Continue with "
                            "other work; do not retry this action."
                        ),
                        "decision_id": str(decision.id),
                    }
                )
            try:
                credentials = provider.credentials_for(tool)
                result = await provider.dispatch(tool, credentials=credentials, kwargs=arguments)
            except Exception as exc:  # noqa: BLE001 — surface to LLM, never crash the loop
                logger.warning(
                    "connector_action_dispatch_failed",
                    run_id=str(run.id),
                    connector=tool.connector,
                    action=tool.action_name,
                    error=str(exc),
                )
                return json.dumps(
                    {
                        "status": "error",
                        "connector": tool.connector,
                        "action": tool.action_name,
                        "error": str(exc),
                    }
                )
            logger.info(
                "connector_action_dispatched",
                run_id=str(run.id),
                connector=tool.connector,
                action=tool.action_name,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "connector": tool.connector,
                    "action": tool.action_name,
                    "result": result,
                },
                default=str,
            )

        return handler

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        project_id = run.product_id or run.id
        work_step = WorkStep(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            title=_intent_title(run),
            status=WorkStepStatus.RUNNING,
            proof_state=ProofState.UNTESTED,
            payload={},
        )
        attempt = RunAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            phase=RunAttemptPhase.PLANNING,
            payload={},
        )
        self._session.add_all([work_step, attempt])
        await self._session.flush()

        try:
            box = await self._sandbox_manager.acquire(project_id, str(workspace_dir))
        except Exception as exc:  # noqa: BLE001 — infra failure → system_error
            await self._record(run, attempt, "error", {"stage": "acquire", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.warning(
                "run_orchestrator_sandbox_unavailable", run_id=str(run.id), error=str(exc)
            )
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"sandbox unavailable: {exc}",
            )

        try:
            return await self._drive_loop(
                run=run,
                work_step=work_step,
                attempt=attempt,
                box=box,
                workspace_dir=workspace_dir,
            )
        except Exception as exc:  # noqa: BLE001 — any loop crash → system_error, never leak
            await self._record(run, attempt, "error", {"stage": "loop", "error": str(exc)})
            attempt.phase = RunAttemptPhase.FAILED
            work_step.status = WorkStepStatus.FAILED
            await self._session.flush()
            logger.exception("run_orchestrator_loop_crash", run_id=str(run.id))
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"loop crashed: {exc}",
            )
        finally:
            await self._sandbox_manager.release(project_id)

    async def _drive_loop(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        box: SandboxSession,
        workspace_dir: Path,
    ) -> LoopResult:
        registry = ToolRegistry(workspace_dir=workspace_dir, sandbox=box)
        # B5a — register the skill + knowledge tools (when a workspace skill
        # loader was threaded in) so the work LLM can discover skills + consult
        # canonical knowledge during the run. Returns the extra surfaced names.
        extra_tool_names = self._register_knowledge_tools(registry)
        # B5b — register the workspace's available connector actions as loop
        # tools (when a connector-action provider was threaded in). Each handler
        # is gated by DangerAnalyzer + workspace safe_mode. No provider / no
        # connector accounts → empty list (loop unchanged).
        connector_tool_names = await self._register_connector_action_tools(
            registry, run=run, work_step=work_step
        )
        tools_schema = [
            *registry.schema_for([*WORK_TOOLS, *extra_tool_names, *connector_tool_names]),
            ASK_USER_QUESTION_TOOL,
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _intent_title(run)},
        ]
        # Resumption context: if the founder resolved a prior blocking question
        # (the run was paused on a Decision and re-opened via /api/v1/checkpoints),
        # seed each resolution so the loop continues WITH that decision in
        # context rather than re-asking. See backend.api.v1.checkpoints.
        messages.extend(_resumption_messages(run))
        attempt.phase = RunAttemptPhase.WORKING
        await self._session.flush()

        written_paths: list[str] = []
        final_text = ""
        no_work_nudges = 0

        for _cycle in range(self._max_cycles):
            turn = await self._llm.complete(messages=messages, tools=tools_schema)
            final_text = turn.content or final_text
            await self._record(
                run,
                attempt,
                "llm_turn",
                {"content": turn.content[:500], "tool_calls": [c.name for c in turn.tool_calls]},
            )

            ask = next((c for c in turn.tool_calls if c.name == "ask_user_question"), None)
            if ask is not None:
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="ask_user_question",
                    payload={
                        "question": str(ask.arguments.get("question") or ""),
                        "context": str(ask.arguments.get("context") or ""),
                    },
                    rationale="work LLM asked the founder a blocking question",
                )
                return self._decision_result(
                    run, work_step, attempt, decision, written_paths, final_text
                )

            if turn.tool_calls:
                messages.append(_assistant_tool_call_message(turn.content, turn.tool_calls))
                for call in turn.tool_calls:
                    output, ok, writes = await _invoke_tool_safely(
                        registry, call.name, call.arguments
                    )
                    if ok:
                        for path in writes:
                            if path not in written_paths:
                                written_paths.append(path)
                    await self._record(
                        run,
                        attempt,
                        "tool_call",
                        {"tool": call.name, "ok": ok, "writes": writes},
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                continue

            # No tool calls: the model believes the step is done.
            if (
                not written_paths
                and registry.declared_contract is None
                and no_work_nudges < MAX_NO_WORK_NUDGES
            ):
                no_work_nudges += 1
                messages.append({"role": "assistant", "content": turn.content or "(no tool calls)"})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have not changed any file or declared a verification contract yet. "
                            "A prose answer is not a deliverable. Use the tools to do the work, then "
                            "declare_verification, then summarise."
                        ),
                    }
                )
                continue

            attempt.phase = RunAttemptPhase.VERIFYING
            await self._session.flush()
            contract = await self._assemble_contract(registry, written_paths, final_text)
            if contract is None:
                # No usable check → never a silent pass (contract.py philosophy).
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="human_review_required",
                    payload={"reason": "no_verification_declared", "written_paths": written_paths},
                    rationale="work finished without any verifiable contract",
                )
                return self._decision_result(
                    run, work_step, attempt, decision, written_paths, final_text
                )

            verdict = await self._verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=written_paths,
                final_text=final_text,
            )
            if verdict.outcome is VerificationOutcome.PASSED:
                return await self._finish_verified(
                    run, work_step, attempt, written_paths, final_text, verdict
                )
            # Failed: feed the verifier output back and re-plan on the next cycle.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Verification FAILED. Details:\n"
                        f"{json.dumps(verdict.result)[:1500]}\n"
                        "Fix the problem and try again, then send your summary."
                    ),
                }
            )

        # Cycle cap reached without a passing verdict → stuck → Decision (§6).
        decision = await self._create_decision(
            run,
            work_step,
            kind="verification_failed",
            payload={"reason": "round_cap_reached", "written_paths": written_paths},
            rationale="agent loop exhausted its round budget without a passing verification",
        )
        return self._decision_result(run, work_step, attempt, decision, written_paths, final_text)

    # -- verify (delegated to the shared VerificationService) --------------

    def _verifier(self) -> VerificationService:
        """Build the shared verifier with this run's deps. The native loop and
        the executor orchestrator both go through this SAME service so they
        verify identically (Lift B2a)."""
        return VerificationService(session=self._session, llm=self._llm, retriever=self._retriever)

    async def _verify(
        self,
        *,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        contract: VerificationContract,
        box: SandboxSession,
        written_paths: list[str],
        final_text: str,
    ) -> VerificationResult:
        return await self._verifier().verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=written_paths,
            final_text=final_text,
        )

    async def _assemble_contract(
        self, registry: ToolRegistry, written_paths: list[str], final_text: str
    ) -> VerificationContract | None:
        return await self._verifier().assemble_contract(
            declared_contract=registry.declared_contract,
            written_paths=written_paths,
            final_text=final_text,
        )

    # -- terminal helpers --------------------------------------------------

    async def _finish_verified(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        written_paths: list[str],
        final_text: str,
        verdict: VerificationResult,
    ) -> LoopResult:
        work_step.status = WorkStepStatus.VERIFIED
        work_step.proof_state = ProofState.PROVED
        attempt.phase = RunAttemptPhase.COMPLETED
        attempt.finished_at = _utcnow()

        # The verified-terminal artifact contract (Deliverable type CODE +
        # DeliveryEventRow + settle activity) is the SAME regardless of compute
        # backend, so it lives in ONE shared helper (Lift 5b). The settle payload
        # carries the run's STABLE context (product binding + founder intent_text)
        # so the SettleWorker can cluster garden observations by product + intent
        # — deterministic inputs, never the work LLM's free output.
        deliverable = await write_verified_deliverable(
            self._session,
            run,
            attempt_id=attempt.id,
            artifact_refs=written_paths,
            summary=final_text,
        )

        # Wake the delivery + settle consumers (worker_mode="redis_streams"
        # only). The DeliveryEventRow + settle ExecutionRunActivity are the
        # source of truth — already flushed above; the XADD is only a wake-up so
        # the consumer ticks immediately instead of waiting for the next DB poll.
        # Gated (no-op + no Redis touched in db_polling — the default) and
        # soft-fail (a Redis hiccup never reverts the verified terminal: emission
        # only logs + returns False). DB-polling remains the safety net. The
        # emit helper is imported LOCALLY (``backend.workers`` pulls in
        # ``agent_worker`` which imports this module → a module-level import
        # would be a cycle; the local import breaks it, same as the
        # ``DeliveryEventRow`` import above).
        from backend.workers.emit import (  # noqa: PLC0415 — cross-domain, breaks import cycle
            STREAM_DELIVER,
            STREAM_SETTLE,
            emit_stream_notification,
        )

        await emit_stream_notification(
            self._redis_client,
            settings=self._settings,
            stream=STREAM_DELIVER,
            fields={"workspace_id": str(run.workspace_id), "deliverable_id": str(deliverable.id)},
        )
        await emit_stream_notification(
            self._redis_client,
            settings=self._settings,
            stream=STREAM_SETTLE,
            fields={"workspace_id": str(run.workspace_id), "run_id": str(run.id)},
        )

        logger.info(
            "run_orchestrator_verified",
            run_id=str(run.id),
            artifact_refs=written_paths,
        )
        return LoopResult(
            outcome="verified",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            verification_result_id=verdict.id,
            written_paths=written_paths,
            summary=final_text,
        )

    async def _create_decision(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str,
    ) -> Decision:
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            decision=kind,
            actor_id=None,
            rationale=rationale,
            payload=payload,
        )
        self._session.add(decision)
        await self._session.flush()
        logger.info("run_orchestrator_needs_decision", run_id=str(run.id), kind=kind)
        return decision

    def _decision_result(
        self,
        run: ExecutionRun,
        work_step: WorkStep,
        attempt: RunAttempt,
        decision: Decision,
        written_paths: list[str],
        final_text: str,
    ) -> LoopResult:
        return LoopResult(
            outcome="needs_decision",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            decision_id=decision.id,
            written_paths=written_paths,
            summary=final_text,
        )

    async def _record(
        self,
        run: ExecutionRun,
        attempt: RunAttempt,
        activity_type: str,
        payload: dict[str, Any],
    ) -> None:
        self._session.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type=activity_type,
                payload={"attempt_id": str(attempt.id), **payload},
            )
        )


async def _invoke_tool_safely(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> tuple[str, bool, list[str]]:
    """Run ``registry.invoke`` and translate failures into a string the
    LLM can read. Returns (output, ok, write_paths)."""
    writes: list[str] = []
    if name in ("file_write", "file_edit"):
        path = arguments.get("path")
        if isinstance(path, str):
            writes.append(path)
    try:
        output = await registry.invoke(name, arguments)
        return output, True, writes
    except ToolError as exc:
        return f"ERROR: {exc}", False, writes


def _assistant_tool_call_message(
    content: str, tool_calls: tuple[LoopToolCall, ...]
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in tool_calls
        ],
    }


def _connector_action_schema(tool: ConnectorActionTool) -> dict[str, Any]:
    """The OpenAI-style parameters schema for a connector action tool.

    Reuses the action's declared ``input_schema`` (validated by the runner on
    dispatch) when present; otherwise an open object so the LLM can still pass
    arguments through to a schema-less action."""
    schema = tool.action.input_schema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _intent_title(run: ExecutionRun) -> str:
    payload = run.payload or {}
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:512]


def _resumption_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """Build loop seed messages for any founder-resolved decisions.

    ``run.payload["resolved_decisions"]`` is a list of
    ``{decision_id, question, answer}`` appended by the checkpoints resolve
    endpoint. Each becomes a user message so the work LLM continues with the
    founder's answer in context instead of re-asking the blocking question."""
    payload = run.payload or {}
    resolved = payload.get("resolved_decisions") if isinstance(payload, dict) else None
    if not isinstance(resolved, list):
        return []
    messages: list[dict[str, Any]] = []
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not answer:
            continue
        messages.append(
            {
                "role": "user",
                "content": (
                    "The founder resolved a prior question — "
                    f"Q: {question} A: {answer}. "
                    "Continue the work with this decision."
                ),
            }
        )
    return messages


def _utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "KNOWLEDGE_SEARCH_NAME",
    "WORK_TOOLS",
    "CanonRetriever",
    "ConnectorActionProvider",
    "ConnectorActionTool",
    "LoopLlm",
    "LoopOutcome",
    "LoopResult",
    "LoopToolCall",
    "LoopTurn",
    "RunCompute",
    "RunOrchestrator",
]
