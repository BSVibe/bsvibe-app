"""Workflow context — application layer.

H1 placeholder. H2a populates the loop conductor + the three sibling
modules it leans on (tool_registry, connector_action_registrar,
run_persistence); H2b absorbs the legacy ``orchestrator/`` stage machine;
H2c implements the transition handlers; H3 absorbs ``intake/`` +
``delivery/`` into ``application/stages/``.

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

__all__ = [
    "CanonRetriever",
    "LoopLlm",
    "LoopOutcome",
    "LoopResult",
    "LoopToolCall",
    "LoopTurn",
    "RunCompute",
    "RunOrchestrator",
]
