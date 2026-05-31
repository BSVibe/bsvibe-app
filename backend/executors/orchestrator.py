"""Backward-compat shim — re-exports the public surface after the Lift D
§17.8 4-file split. New code should import directly from the source modules.

The 4-file split (Lift D):

* :mod:`backend.executors.coordinator` — :class:`ExecutorOrchestrator` + dispatch flow.
* :mod:`backend.executors.prompt` — B8 prompt assembly + system prompt + utils.
* :mod:`backend.executors.terminal` — fail/decision/audit shared helpers.
* :mod:`backend.executors.verify_handoff` — B2b verification convergence.

Pre-split callers (``from backend.executors.orchestrator import
ExecutorOrchestrator`` + tests reaching for ``_parse_uuid`` / prompt helpers)
keep working through this shim. Will be removed in a follow-up once all
in-repo callers move to the new modules.
"""

from __future__ import annotations

from backend.executors.coordinator import (
    DECISION_NO_DISPATCH_TRANSPORT,
    DECISION_NO_WORKER_AVAILABLE,
    ExecutorOrchestrator,
)
from backend.executors.prompt import (
    _DESIGN_SPEC_DIRECTIVE,
    _EXECUTOR_SYSTEM_PROMPT,
    _INTENT_MAX_CHARS,
    _KNOWLEDGE_MAX_CHARS_PER_STATEMENT,
    _KNOWLEDGE_MAX_RESULTS,
    _assemble_executor_prompt,
    _executor_system_prompt,
    _intent_text,
    _is_design_stage,
    _parse_uuid,
    _resolved_decisions,
)
from backend.executors.verify_handoff import (
    DECISION_HUMAN_REVIEW_REQUIRED,
    DECISION_VERIFICATION_FAILED,
)

__all__ = [
    "DECISION_HUMAN_REVIEW_REQUIRED",
    "DECISION_NO_DISPATCH_TRANSPORT",
    "DECISION_NO_WORKER_AVAILABLE",
    "DECISION_VERIFICATION_FAILED",
    "ExecutorOrchestrator",
    "_DESIGN_SPEC_DIRECTIVE",
    "_EXECUTOR_SYSTEM_PROMPT",
    "_INTENT_MAX_CHARS",
    "_KNOWLEDGE_MAX_CHARS_PER_STATEMENT",
    "_KNOWLEDGE_MAX_RESULTS",
    "_assemble_executor_prompt",
    "_executor_system_prompt",
    "_intent_text",
    "_is_design_stage",
    "_parse_uuid",
    "_resolved_decisions",
]
