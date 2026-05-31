"""Workflow context — application layer.

H1 placeholder. H2a populates the loop conductor + the three sibling
modules it leans on (tool_registry, connector_action_registrar,
run_persistence); H2b absorbs the legacy ``orchestrator/`` stage machine;
H2c relocates :class:`AgentRunner` here and lands the transition
handlers + state machine driver; H3 absorbs ``intake/`` + ``delivery/``
into ``application/stages/``.

D36 invariant — external callers MUST import from this package (or its
submodules) only. The legacy ``backend.execution.orchestrator`` shim
re-exports the H2a surface during the migration window.
"""

from __future__ import annotations

from backend.workflow.application.agent_loop import (
    CanonRetriever,
    LoopLlm,
    LoopOutcome,
    LoopResult,
    LoopToolCall,
    LoopTurn,
    RunCompute,
    RunOrchestrator,
)
from backend.workflow.application.agent_runner import AgentRunner

__all__ = [
    "AgentRunner",
    "CanonRetriever",
    "LoopLlm",
    "LoopOutcome",
    "LoopResult",
    "LoopToolCall",
    "LoopTurn",
    "RunCompute",
    "RunOrchestrator",
]
