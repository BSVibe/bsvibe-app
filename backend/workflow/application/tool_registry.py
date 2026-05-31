"""Loop tool registry — schemas + dispatch helpers for the work LLM.

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1). Holds
the static set of work-tool names (``WORK_TOOLS``), the schemas for the
loop-owned pseudo-tools (``ask_user_question``), the round-cap nudge
budget, and the small helpers that translate a ``ToolRegistry.invoke``
into a string the LLM can read.

The ``ToolRegistry`` *class* itself still lives in
:mod:`backend.execution.tools` (the sandboxed file-write surface — out of
scope for H2a). This module is the loop-side companion: the things the
agent loop reaches for when it wires schemas + dispatches the LLM's
chosen tool calls.
"""

from __future__ import annotations

import json
from typing import Any

from backend.execution.tools import ToolError, ToolRegistry

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
# (``backend.execution.verifier.service``) — the canonical home shared by both
# the native loop and the executor orchestrator.
MAX_NO_WORK_NUDGES = 2


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
    "WORK_TOOLS",
    "_KNOWLEDGE_SEARCH_MAX_RESULTS",
    "_KNOWLEDGE_SEED_MAX_CHARS_PER_STATEMENT",
    "_KNOWLEDGE_SEED_MAX_RESULTS",
    "_assistant_tool_call_message",
    "_invoke_tool_safely",
    "_sanitize_ask_user_question_options",
]
