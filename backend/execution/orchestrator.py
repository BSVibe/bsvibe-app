"""Back-compat shim — RunOrchestrator now lives in the Workflow context.

Lift H2a (v8 §17.1) decomposed this 1601 LOC god-file into 5 focused
modules under :mod:`backend.workflow`:

* :mod:`backend.workflow.application.agent_loop` — the loop conductor
  (Protocols, ``LoopResult``, ``RunOrchestrator``, message assembly).
* :mod:`backend.workflow.application.tool_registry` — ``WORK_TOOLS``,
  ``ASK_USER_QUESTION_TOOL``, the nudge budget, dispatch helpers.
* :mod:`backend.workflow.application.connector_action_registrar` —
  workspace connector-action tool registration.
* :mod:`backend.workflow.application.run_persistence` — DB-side helpers
  (activity rows, decisions, verified-terminal, audit emit).
* :mod:`backend.workflow.domain.emit_deliverable` — mid-loop Deliver
  events (D6 / B12a).

This shim re-exports the names every external caller + glue test still
depends on so the mechanical caller-update can be done piecemeal. New
code MUST import from ``backend.workflow.application`` per the v8 D36
public-surface invariant.
"""

from __future__ import annotations

from backend.execution.connector_actions import (
    ConnectorActionProvider,
    ConnectorActionTool,
)
from backend.workflow.application.agent_loop import (
    _DESIGN_SPEC_DIRECTIVE,
    _SYSTEM_PROMPT,
    CanonRetriever,
    LoopLlm,
    LoopOutcome,
    LoopResult,
    LoopToolCall,
    LoopTurn,
    RunCompute,
    RunOrchestrator,
    _intent_title,
    _is_design_stage,
    _resumption_messages,
)
from backend.workflow.application.tool_registry import (
    ASK_USER_QUESTION_TOOL,
    KNOWLEDGE_SEARCH_NAME,
    MAX_NO_WORK_NUDGES,
    WORK_TOOLS,
    _assistant_tool_call_message,
    _invoke_tool_safely,
    _sanitize_ask_user_question_options,
)
from backend.workflow.domain.emit_deliverable import (
    EMIT_DELIVERABLE_NAME,
    EMIT_DELIVERABLE_TOOL,
    _safe_args,
)

__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "EMIT_DELIVERABLE_NAME",
    "EMIT_DELIVERABLE_TOOL",
    "KNOWLEDGE_SEARCH_NAME",
    "MAX_NO_WORK_NUDGES",
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
    "_DESIGN_SPEC_DIRECTIVE",
    "_SYSTEM_PROMPT",
    "_assistant_tool_call_message",
    "_intent_title",
    "_invoke_tool_safely",
    "_is_design_stage",
    "_resumption_messages",
    "_safe_args",
    "_sanitize_ask_user_question_options",
]
