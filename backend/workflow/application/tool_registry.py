"""Loop tool registry — schemas + dispatch helpers for the work LLM.

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1). Holds
the static set of work-tool names (``WORK_TOOLS``), the schemas for the
loop-owned pseudo-tools (``ask_user_question``), the round-cap nudge
budget, and the small helpers that translate a ``ToolRegistry.invoke``
into a string the LLM can read.

The ``ToolRegistry`` *class* itself still lives in
:mod:`backend.workflow.infrastructure.tools` (the sandboxed file-write surface — out of
scope for H2a). This module is the loop-side companion: the things the
agent loop reaches for when it wires schemas + dispatches the LLM's
chosen tool calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from backend.workflow.infrastructure.sandbox import SandboxSession
from backend.workflow.infrastructure.tools import ToolDefinition, ToolError, ToolRegistry

logger = structlog.get_logger(__name__)

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
# ``CanonRetriever`` the verifier folds in (B3). ``invoke_skill`` is registered
# separately via :func:`register_invoke_skill`. Both are only surfaced when the
# orchestrator was given a workspace ``skill_loader`` (the production worker
# factory always threads one in; legacy/test callers that omit it keep the
# original 6-tool set).
KNOWLEDGE_SEARCH_NAME = "knowledge_search"
_KNOWLEDGE_SEARCH_MAX_RESULTS = 5

# B6 — at loop start, canon relevant to the run's intent is SEEDED into the
# agent's initial context so the work is informed by prior knowledge (not just
# the verify-time fold of B3). Capped: top-N statements, each clamped, so the
# seed never blows the (local-model) generation budget. Empty / no retriever →
# no seed message at all (empty-knowledge workspace = byte-identical to today).
_KNOWLEDGE_SEED_MAX_RESULTS = 5
_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT = 500

# The per-command verify timeout, the judge file-context cap, and the judge
# verdict parser now live with the shared VerificationService
# (``backend.workflow.application.verification_service``) — the canonical home shared by both
# the native loop and the executor orchestrator.
MAX_NO_WORK_NUDGES = 2


#: (MCP tool name, inner registry tool name) for the run-scoped tools that FORWARD to the inner
#: ``ToolRegistry``. This is the SINGLE source of truth tying the surface the MCP server exposes
#: to the tools the shared factory registers to the allowlist the dispatch adapter advertises to
#: the CLI. It lives HERE — the clean loop-side module — precisely so the MCP transport
#: (``work_tools``) and the dispatch adapter can both read it WITHOUT importing each other
#: (importing ``backend.mcp`` from the adapter is a circular import through the whole API graph).
#: INV-7 #2: advertised ≡ registered because both are derived from this one tuple.
RUN_TOOL_FORWARDING: tuple[tuple[str, str], ...] = (
    ("bsvibe_work_file_read", "file_read"),
    ("bsvibe_work_file_list", "file_list"),
    ("bsvibe_work_file_write", "file_write"),
    ("bsvibe_work_file_edit", "file_edit"),
    ("bsvibe_work_shell_exec", "shell_exec"),
    ("bsvibe_work_declare_verification", "declare_verification"),
    ("bsvibe_work_knowledge_search", KNOWLEDGE_SEARCH_NAME),
)

#: The two LOOP-owned pseudo-tools — the MCP transport handles them directly (create a Decision /
#: record a mid-run deliverable); they do NOT forward to the inner registry.
RUN_TOOL_LOOP_OWNED: tuple[str, ...] = (
    "bsvibe_work_ask_user_question",
    "bsvibe_work_emit_deliverable",
)

#: The COMPLETE run-scoped MCP surface a task token may see — what the dispatch adapter advertises
#: as the CLI ``--allowedTools`` allowlist (``WORK_TOOL_NAMES``) and what the MCP server offers a
#: run-scoped principal. Derived, never hand-kept.
WORK_TOOL_MCP_NAMES: tuple[str, ...] = (
    *(name for name, _ in RUN_TOOL_FORWARDING),
    *RUN_TOOL_LOOP_OWNED,
)

#: The inner-registry tools BOTH transports invoke — the forwarding targets. INV-7 #1: this is
#: what the shared factory (:func:`assemble_run_tool_registry`) must register, so the MCP
#: transport and the in-process loop cannot drift on it (``knowledge_search`` was advertised by
#: the MCP layer while its per-request registry never registered it → ``Unknown tool`` on every
#: call, executor RAG grounding 0, measured live). ``invoke_skill`` and connector actions are NOT
#: here: their registration code lives in ``backend.extensions`` / ``backend.connectors``, which
#: the MCP context is forbidden to import, so the worker adds them AFTER this factory (see
#: ``_drive_loop``). They ride the worker path only until a follow-up threads their deps in from
#: the composition root.
RUN_TOOL_INNER_NAMES: tuple[str, ...] = tuple(inner for _, inner in RUN_TOOL_FORWARDING)


def make_knowledge_search_handler(retriever: Any) -> Any:
    """Build the ``knowledge_search`` tool handler bound to ``retriever``.

    Never raises into the loop. Returns a human-legible string of the top canonical statements
    for the query, or a valid empty result when there is no knowledge / no retriever / the
    retrieval fails. (Lives here — the clean loop-side module — so the MCP transport can register
    the same handler; ``_loop_context`` re-exports it for the worker's existing callers.)"""

    async def handler(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "knowledge_search requires a non-empty 'query'."
        if retriever is None:
            return "No workspace knowledge is available."
        try:
            statements = await retriever.retrieve_for_signals(query)
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

    return handler


def register_knowledge_search(registry: ToolRegistry, retriever: Any) -> None:
    """Register the read-only ``knowledge_search`` tool into ``registry``.

    Always safe to register: with no retriever (or a failing one) the handler degrades to
    "No workspace knowledge is available" — it never gates a write and never raises. Kept
    separate from ``invoke_skill`` (which needs a ``SkillLoader`` from ``backend.extensions``)
    so this — the tool with no forbidden-context dependency — can be part of the shared factory
    both transports call."""
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
            handler=make_knowledge_search_handler(retriever),
        )
    )


def assemble_run_tool_registry(
    *, workspace_dir: Path, sandbox: SandboxSession | None, retriever: Any = None
) -> ToolRegistry:
    """The ONE builder of a run's inner ``ToolRegistry`` (INV-7 #1).

    Both the MCP transport (:func:`backend.mcp.tools.work_registry.build_run_tool_registry`) and
    the in-process loop (``_drive_loop``) call this, so the run-scoped tool set they invoke can
    never diverge on the shared tools. It binds the six sandbox tools (registered by the registry
    itself) plus the ``knowledge_search`` consult, all against the run's SERVER-SIDE worktree +
    sandbox. Worker-only additions (``invoke_skill``, connector actions) are layered on by the
    caller AFTER this returns — see ``RUN_TOOL_INNER_NAMES``."""
    registry = ToolRegistry(workspace_dir=workspace_dir, sandbox=sandbox)
    register_knowledge_search(registry, retriever)
    return registry


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


#: Where a run carries the work-tool registry's per-run state (``ToolRegistry.export_state``).
#:
#: The in-process loop keeps its registry in memory. The MCP transport builds one PER REQUEST,
#: in ANOTHER PROCESS (the API container), so the run row is the only channel between them —
#: both for what the agent declared and for what it wrote. Defined here, in the workflow layer,
#: because it is a property of the RUN: ``backend.mcp`` imports it, never the other way round.
WORK_TOOL_STATE_KEY = "work_tool_state"


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


def _assistant_tool_call_message(content: str, tool_calls: Any) -> dict[str, Any]:
    """Build the assistant message persisting the LLM's tool-call turn.

    ``tool_calls`` is a tuple[LoopToolCall, ...] (kept ``Any`` to avoid an
    import cycle with ``agent_loop`` where ``LoopToolCall`` lives)."""
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


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "KNOWLEDGE_SEARCH_NAME",
    "MAX_NO_WORK_NUDGES",
    "RUN_TOOL_FORWARDING",
    "RUN_TOOL_INNER_NAMES",
    "RUN_TOOL_LOOP_OWNED",
    "WORK_TOOLS",
    "WORK_TOOL_MCP_NAMES",
    "_KNOWLEDGE_SEARCH_MAX_RESULTS",
    "_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT",
    "_KNOWLEDGE_SEED_MAX_RESULTS",
    "_assistant_tool_call_message",
    "_invoke_tool_safely",
    "_sanitize_ask_user_question_options",
    "assemble_run_tool_registry",
    "make_knowledge_search_handler",
    "register_knowledge_search",
]
