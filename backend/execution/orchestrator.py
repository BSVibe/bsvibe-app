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
from backend.execution.audit_events import (
    DecisionPending,
    LlmTurn,
    LoopTerminal,
    RunStarted,
    ToolCall,
    VerifyRun,
)
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
from backend.execution.verified_deliverable import (
    write_partial_deliverable,
    write_verified_deliverable,
)
from backend.execution.verifier.contract import (
    VerificationContract,
)
from backend.execution.verifier.service import VerificationService
from backend.skills.loader import SkillLoader
from backend.skills.tool_binding import INVOKE_SKILL_NAME, register_invoke_skill
from backend.supervisor.audit.events import AuditActor, AuditEventBase, AuditResource
from backend.supervisor.audit.service import safe_emit
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

# B6 — at loop start, canon relevant to the run's intent is SEEDED into the
# agent's initial context so the work is informed by prior knowledge (not just
# the verify-time fold of B3). Capped: top-N statements, each clamped, so the
# seed never blows the (local-model) generation budget. Empty / no retriever →
# no seed message at all (empty-knowledge workspace = byte-identical to today).
_KNOWLEDGE_SEED_MAX_RESULTS = 5
_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT = 500

EMIT_DELIVERABLE_NAME = "emit_deliverable"

EMIT_DELIVERABLE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EMIT_DELIVERABLE_NAME,
        "description": (
            "Emit ONE external artifact you produced during this run — a partial "
            "Deliver event (B12a / Workflow §1). Use this whenever you have just "
            "produced one external thing (a PR, an issue comment, a Notion page, "
            "a draft) so the founder sees it on the Brief and Safe Mode can hold "
            "it for approval. Multi-artifact is the norm: emit ONE call per "
            "artifact (do NOT bundle several artifacts into one emit). This does "
            "NOT replace your verification — keep going through declare_verification "
            "+ tools + summary as usual; emit_deliverable is purely a side-channel "
            "for what already exists externally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_type": {
                    "type": "string",
                    "description": (
                        "What kind of artifact this is — e.g. 'pr', 'issue_comment', "
                        "'notion_page', 'page', 'page_image', 'direct_output'."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": ("A founder-readable one-liner — what you delivered and why."),
                },
                "external_ref": {
                    "type": "string",
                    "description": (
                        "Plugin-canonical id for the external artifact (used to "
                        "dedupe re-emits and as the compensation key). Example: "
                        "'github://acme/site/pull/15'. Optional but strongly "
                        "preferred — re-emitting the same external_ref is a no-op."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "Where the artifact landed — 'github', 'notion', 'slack', etc. Optional."
                    ),
                },
            },
            "required": ["artifact_type", "summary"],
        },
    },
}


ASK_USER_QUESTION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user_question",
        "description": (
            "Pause the run and ask the founder a blocking question when you "
            "genuinely cannot proceed without a human decision. This creates a "
            "Decision and stops the loop until it is resolved — use it only when "
            "no tool call can unblock you. When the decision is a choice between "
            "concrete alternatives, pass them as ``options`` (a list of plain "
            "strings) so the founder sees those choices as suggestions. The "
            "options are NOT a closed set — the founder may pick one of them or "
            'type a different answer ("Other" free-text). Offer the 2–4 most '
            "likely choices you would consider; do not try to enumerate every "
            "possibility."
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
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional concrete suggestions to present to the founder. "
                        "When set, the PWA shows them as a single-select with an "
                        '"Other" option for free-text. The founder\'s answer '
                        "is recorded verbatim — do not assume membership."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}


def _sanitize_ask_user_question_options(raw: Any) -> list[str] | None:
    """Coerce the work LLM's ``options`` arg into a clean ``list[str]``.

    B11a: only plain non-empty strings survive; anything else (numbers, ``None``,
    whitespace-only, the wrong outer type) is dropped. Returns ``None`` when
    nothing usable remains so the Decision payload simply omits the field — the
    resolve endpoint then treats the question as free-text (existing behaviour).
    """
    if not isinstance(raw, list):
        return None
    cleaned: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            cleaned.append(entry)
    return cleaned or None


_SYSTEM_PROMPT = (
    "You are an autonomous engineer working inside a sandboxed workspace. "
    "Use the tools to inspect and change files. You MUST call "
    "declare_verification BEFORE any file_write or file_edit — those tools are "
    "REFUSED until you do — to commit to how the work will be checked (prefer a "
    "command check that runs the real test/lint, scoped to the files you "
    "changed). Reading files (file_read, file_list) is allowed first. When the "
    "step is complete, stop calling tools and reply with a short plain-text "
    "summary — that triggers verification. If you are blocked on a decision "
    "only the founder can make, call ask_user_question. "
    "W2 — your work is committed to a per-run git branch and merged into the "
    "product's main on verify. If verify reports a merge conflict, the "
    "conflicting files in your workspace will contain '<<<<<<<', '=======', "
    "and '>>>>>>>' markers. Resolve them with file_read/file_edit (you can "
    "also `shell_exec git log/diff/show` to inspect main's intent) and "
    "re-trigger verification by re-replying. If the conflict is semantically "
    "ambiguous — i.e. you can't tell which intent to honor — call "
    "ask_user_question with a clear semantic question (e.g., 'main added X "
    "while this branch added Y at the same spot — should X replace Y, or "
    "should both coexist?'). Never paste raw conflict markers to the founder."
)

# D1b — when a run is the DESIGN stage of a ``design_then_impl`` pipeline, it
# must produce a SPECIFICATION (a concise markdown spec the impl stage
# implements), NOT finished code. Before D1b the design run got only the generic
# work prompt, so it built working code the impl stage regenerated — a no-op
# merge (2026-05-28 dogfood). This directive, seeded into the loop's initial
# context for a design-stage run, redirects it to spec. One concise instruction
# block (respect the local-model generation budget). The ``single`` + ``impl``
# runs never get it (impl IMPLEMENTS the spec). Kept byte-identical to the
# executor path's directive so both prompt-assembly sites tell the design run
# the same thing.
_DESIGN_SPEC_DIRECTIVE = (
    "THIS IS THE DESIGN STAGE. Write ONE concise markdown specification — do NOT "
    "implement it and do NOT write working code; a later implementation stage "
    "will. The spec MUST cover: Goal (what to build and why), "
    "Interface/Contract (the public API, signatures, inputs/outputs), File "
    "layout (the files to create and what each holds), and Acceptance criteria "
    "(observable conditions that prove the implementation is correct). Keep it "
    "tight and implementable; output only the spec."
)


def _is_design_stage(run: ExecutionRun) -> bool:
    """D1b — True when this run is the DESIGN stage of a ``design_then_impl``
    pipeline (so the loop is told to spec, not build).

    Mirrors routing's ``_derive_stage`` + the executor path's ``_is_design_stage``:
    the FIRST run of a ``design_then_impl`` pipeline carries no explicit
    ``stage`` (the AgentRunner chains impl off the frame's pipeline signal), so
    an unset / non-``impl`` stage on such a run IS the design stage. The spawned
    impl run (``stage="impl"``) is excluded — it implements the spec. Any other
    pipeline (``single`` / no frame) is excluded. Tolerant of an odd payload."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    raw_frame = payload.get("frame")
    frame = raw_frame if isinstance(raw_frame, dict) else {}
    if frame.get("pipeline") != "design_then_impl":
        return False
    return payload.get("stage") != "impl"


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
        suggested_skill: str | None = None,
        suggested_skill_description: str | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._sandbox_manager = sandbox_manager
        self._settings = settings or get_settings()
        # B9a — the frame stage's matched skill (+ its description), consumed as a
        # FIRST-INVOCATION hint: the loop's initial context nudges the work LLM to
        # invoke it via ``invoke_skill`` if appropriate. ``None`` (no frame match
        # / legacy caller) → no hint message, loop unchanged. The frame's output
        # was written-but-never-read before B9a; this is where it reaches the loop.
        self._suggested_skill = suggested_skill
        self._suggested_skill_description = suggested_skill_description
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

    async def _knowledge_seed_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        """B6 — build the loop-start knowledge seed for ``run``, or ``None``.

        Retrieves canon relevant to the run's STABLE intent (the same text the
        first user turn uses — never written_paths, none exist yet) and folds the
        top statements into a single context message so the work is informed by
        the workspace's established patterns BEFORE the act/verify cycle. No
        retriever / no patterns → ``None`` (inject nothing; an empty-knowledge
        workspace stays byte-identical to pre-B6). Never raises — a retrieval
        hiccup degrades to no seed, exactly like the B3 verify fold."""
        retriever = self._retriever
        if retriever is None:
            return None
        signals = _intent_title(run)
        try:
            statements = await retriever.retrieve_for_signals(signals)
        except Exception:  # noqa: BLE001 — seeding must never crash the loop
            logger.warning("knowledge_seed_retrieve_failed", run_id=str(run.id), exc_info=True)
            return None
        cleaned = [
            s.strip()[:_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT]
            for s in statements
            if s and s.strip()
        ][:_KNOWLEDGE_SEED_MAX_RESULTS]
        if not cleaned:
            return None
        body = "\n".join(f"- {s}" for s in cleaned)
        logger.info("knowledge_seeded", run_id=str(run.id), count=len(cleaned))
        return {
            "role": "system",
            "content": (
                "Relevant established patterns for this workspace "
                "(consider them as you work):\n" + body
            ),
        }

    def _design_directive_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        """D1b — when this run is the DESIGN stage of a ``design_then_impl``
        pipeline, seed the spec-only directive so the loop writes a spec rather
        than finished code (the impl stage implements it).

        ``None`` for a single run, an impl-stage run, or a run with no frame
        (loop unchanged)."""
        if not _is_design_stage(run):
            return None
        logger.info("design_directive_seeded", run_id=str(run.id))
        return {"role": "system", "content": _DESIGN_SPEC_DIRECTIVE}

    def _design_seed_message(self, run: ExecutionRun) -> dict[str, Any] | None:
        """P1-L2b — fold the prior design stage's spec into the loop-start
        context when this run is the impl stage of a design→impl handoff.

        ``None`` for a non-impl run (no design refs) or when no spec content is
        readable — best-effort, never raises into the loop."""
        from backend.execution.handoff import read_design_context  # noqa: PLC0415

        content = read_design_context(run, self._settings)
        if content is None:
            return None
        logger.info("design_seeded", run_id=str(run.id))
        return {"role": "system", "content": content}

    def _suggested_skill_message(self) -> dict[str, Any] | None:
        """B9a — the frame-matched skill hint for the loop's initial context.

        ``None`` when the frame matched no skill (the hint is omitted, loop
        unchanged). The message names the skill + its description and points the
        work LLM at ``invoke_skill`` — a hint, not a forced first action."""
        name = self._suggested_skill
        if not name:
            return None
        description = self._suggested_skill_description or ""
        suffix = f" — {description}" if description else ""
        return {
            "role": "system",
            "content": (
                f"Suggested skill for this task: {name}{suffix}. "
                f"Invoke it via invoke_skill if appropriate for the work."
            ),
        }

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

        # B15 — emit RunStarted onto the audit outbox the moment the run is
        # known to its WorkStep+RunAttempt rows. Soft-fail: any outbox failure
        # is logged + swallowed (the run continues regardless).
        await self._audit(run, attempt, RunStarted, {"intent": _intent_title(run)})

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
            # B15 — emit a terminal so the audit stream is not blind to bad runs.
            await self._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "system_error", "stage": "acquire", "error": str(exc)},
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
            # B15 — emit a terminal so the audit stream is not blind to bad runs.
            await self._audit(
                run,
                attempt,
                LoopTerminal,
                {"outcome": "system_error", "stage": "loop", "error": str(exc)},
            )
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
            # B12a — mid-loop Deliver events (Workflow §1): the agent loop emits
            # one of these per external artifact (PR / page / comment / draft)
            # produced during the run, BEFORE the verified terminal. The
            # terminal still writes its CODE Deliverable on top — these are
            # additive partials.
            EMIT_DELIVERABLE_TOOL,
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _intent_title(run)},
        ]
        # B6 — seed canon relevant to the run intent into the initial context so
        # the work is informed by the workspace's established patterns up front
        # (the complement to B5a's on-demand knowledge_search + B3's verify-time
        # fold). No retriever / empty knowledge → nothing injected.
        seed = await self._knowledge_seed_message(run)
        if seed is not None:
            messages.append(seed)
        # P1-L2b — design→impl handoff: seed the prior design stage's spec so the
        # impl run implements it. None for a non-impl run (loop unchanged).
        design_seed = self._design_seed_message(run)
        if design_seed is not None:
            messages.append(design_seed)
        # D1b — when THIS run is the DESIGN stage of a design_then_impl pipeline,
        # tell the loop to write a spec, not finished code (else design builds
        # working code the impl stage regenerates → a no-op merge). Mutually
        # exclusive with the impl-side design_seed above. None for single / impl
        # / no-frame runs (loop unchanged).
        design_directive = self._design_directive_message(run)
        if design_directive is not None:
            messages.append(design_directive)
        # B9a — the frame stage's matched skill, seeded as a first-invocation
        # hint. The frame chose this skill by matching the request against the
        # workspace skill catalog (by description); surfacing it here is how the
        # frame output is finally CONSUMED (it was written-but-ignored before).
        # No match → nothing injected (loop unchanged).
        skill_hint = self._suggested_skill_message()
        if skill_hint is not None:
            messages.append(skill_hint)
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
            # B15 — emit one ``LlmTurn`` per completion. Carries the small
            # observable shape (counts + tool names), NEVER the raw LLM
            # content (that stays on the rich ExecutionRunActivity row).
            await self._audit(
                run,
                attempt,
                LlmTurn,
                {
                    "cycle": _cycle,
                    "tool_calls": [c.name for c in turn.tool_calls],
                    "content_len": len(turn.content or ""),
                },
            )

            ask = next((c for c in turn.tool_calls if c.name == "ask_user_question"), None)
            if ask is not None:
                payload: dict[str, Any] = {
                    "question": str(ask.arguments.get("question") or ""),
                    "context": str(ask.arguments.get("context") or ""),
                }
                # B11a: persist structured ``options`` (when the LLM offered
                # concrete choices) so the Decisions UI can render a
                # single-select and the resolve endpoint can validate the
                # founder's answer against them. Sanitised to strings only —
                # noise gets dropped, never planted on disk.
                options = _sanitize_ask_user_question_options(ask.arguments.get("options"))
                if options is not None:
                    payload["options"] = options
                decision = await self._create_decision(
                    run,
                    work_step,
                    kind="ask_user_question",
                    payload=payload,
                    rationale="work LLM asked the founder a blocking question",
                )
                # B15 — DecisionPending + the needs_decision terminal.
                await self._audit(
                    run,
                    attempt,
                    DecisionPending,
                    {
                        "kind": "ask_user_question",
                        "decision_id": str(decision.id),
                        "question": payload.get("question", ""),
                    },
                )
                await self._audit(
                    run,
                    attempt,
                    LoopTerminal,
                    {"outcome": "needs_decision", "decision_id": str(decision.id)},
                )
                return self._decision_result(
                    run, work_step, attempt, decision, written_paths, final_text
                )

            if turn.tool_calls:
                messages.append(_assistant_tool_call_message(turn.content, turn.tool_calls))
                for call in turn.tool_calls:
                    # B12a — emit_deliverable is a LOOP-owned tool (not in the
                    # registry, like ask_user_question). It writes a partial
                    # Deliverable + DeliveryEventRow side-channel and feeds an
                    # ack back to the LLM, then the loop continues. Idempotent
                    # on external_ref; bad args produce a readable error.
                    if call.name == EMIT_DELIVERABLE_NAME:
                        output = await self._handle_emit_deliverable(run, call.arguments)
                        await self._record(
                            run,
                            attempt,
                            "deliver_event",
                            {"tool": call.name, "args": _safe_args(call.arguments)},
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": output}
                        )
                        continue
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
                    # B15 — ToolCall: tool name + ok bool + writes count (no
                    # arguments, no raw output — kept tiny so the outbox stays
                    # cheap; the full payload still lives on activity rows).
                    await self._audit(
                        run,
                        attempt,
                        ToolCall,
                        {"tool": call.name, "ok": ok, "writes_count": len(writes)},
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
                # B15 — DecisionPending + the needs_decision terminal.
                await self._audit(
                    run,
                    attempt,
                    DecisionPending,
                    {
                        "kind": "human_review_required",
                        "decision_id": str(decision.id),
                        "reason": "no_verification_declared",
                    },
                )
                await self._audit(
                    run,
                    attempt,
                    LoopTerminal,
                    {"outcome": "needs_decision", "decision_id": str(decision.id)},
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
            # B15 — VerifyRun: the verdict outcome + counts of the checks the
            # contract actually ran. NEVER the rich verdict result body.
            await self._audit(
                run,
                attempt,
                VerifyRun,
                {
                    "outcome": verdict.outcome.value,
                    "command_checks": len(contract.command_checks),
                    "judge_checks": len(contract.judge_checks),
                },
            )
            if verdict.outcome is VerificationOutcome.PASSED:
                result = await self._finish_verified(
                    run, work_step, attempt, written_paths, final_text, verdict
                )
                # B15 — terminal (verified) — the founder-facing closing event.
                await self._audit(
                    run,
                    attempt,
                    LoopTerminal,
                    {"outcome": "verified", "written_paths_count": len(written_paths)},
                )
                return result
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
        # B15 — DecisionPending + the needs_decision terminal.
        await self._audit(
            run,
            attempt,
            DecisionPending,
            {
                "kind": "verification_failed",
                "decision_id": str(decision.id),
                "reason": "round_cap_reached",
            },
        )
        await self._audit(
            run,
            attempt,
            LoopTerminal,
            {"outcome": "needs_decision", "decision_id": str(decision.id)},
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

    async def _handle_emit_deliverable(self, run: ExecutionRun, arguments: dict[str, Any]) -> str:
        """Persist a mid-loop Deliver event (B12a / Workflow §1).

        Validates the required ``artifact_type`` + ``summary`` strings, calls
        :func:`write_partial_deliverable`, and returns a JSON ack the LLM can
        read. Bad args produce a readable error tool result (the loop can
        recover); persistence failures degrade to an error string but never
        crash the loop."""
        artifact_type = str(arguments.get("artifact_type") or "").strip()
        summary = str(arguments.get("summary") or "").strip()
        external_ref_raw = arguments.get("external_ref")
        channel_raw = arguments.get("channel")
        external_ref = (
            str(external_ref_raw).strip()
            if isinstance(external_ref_raw, str) and external_ref_raw.strip()
            else None
        )
        channel = (
            str(channel_raw).strip()
            if isinstance(channel_raw, str) and channel_raw.strip()
            else None
        )
        if not artifact_type:
            return json.dumps(
                {
                    "status": "error",
                    "error": "emit_deliverable requires a non-empty 'artifact_type'.",
                }
            )
        if not summary:
            return json.dumps(
                {
                    "status": "error",
                    "error": "emit_deliverable requires a non-empty 'summary'.",
                }
            )
        try:
            deliverable = await write_partial_deliverable(
                self._session,
                run,
                artifact_type=artifact_type,
                summary=summary,
                external_ref=external_ref,
                channel=channel,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the loop
            logger.warning(
                "emit_deliverable_failed",
                run_id=str(run.id),
                artifact_type=artifact_type,
                error=str(exc),
            )
            return json.dumps({"status": "error", "error": str(exc)})
        if deliverable is None:
            return json.dumps(
                {
                    "status": "deduped",
                    "artifact_type": artifact_type,
                    "external_ref": external_ref,
                    "message": (
                        "This external_ref was already emitted earlier this run — "
                        "the second emit is a no-op (idempotent). Do not retry."
                    ),
                }
            )
        return json.dumps(
            {
                "status": "emitted",
                "deliverable_id": str(deliverable.id),
                "artifact_type": artifact_type,
            }
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

    # -- B15: audit-stream emit (always soft-fail) -------------------------

    async def _audit(
        self,
        run: ExecutionRun,
        attempt: RunAttempt | None,
        event_cls: type[AuditEventBase],
        data: dict[str, Any],
    ) -> None:
        """Emit one audit event onto the supervisor outbox (B15).

        The supervisor :class:`backend.workers.relay_worker.RelayWorker` drains
        the outbox onto the audit stream — exactly the same seam the gateway
        chat path uses. ``safe_emit`` swallows any emitter failure so the run
        is NEVER broken by audit infrastructure trouble (the soft-fail contract
        every audit producer follows).
        """
        actor = AuditActor(type="system", id="backend.execution.run_orchestrator")
        resource = AuditResource(type="execution_run", id=str(run.id))
        full_data: dict[str, Any] = {
            "run_id": str(run.id),
            "product_id": str(run.product_id) if run.product_id is not None else None,
        }
        if attempt is not None:
            full_data["attempt_id"] = str(attempt.id)
        full_data.update(data)
        event = event_cls(
            actor=actor,
            workspace_id=str(run.workspace_id),
            resource=resource,
            data=full_data,
        )
        await safe_emit(event, session=self._session)


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Truncate tool arguments for activity-payload logging (B12a).

    The agent-activity log carries each tool-call's args for replay/audit;
    long ``summary`` strings would balloon row sizes. Strings over 256 chars
    are truncated with an ellipsis."""
    capped: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > 256:
            capped[k] = v[:253] + "..."
        else:
            capped[k] = v
    return capped


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
    "EMIT_DELIVERABLE_NAME",
    "EMIT_DELIVERABLE_TOOL",
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
