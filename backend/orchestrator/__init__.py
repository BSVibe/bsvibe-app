"""Orchestrator — H2c carry-over only.

Pre-H2b this package hosted the 4-stage state machine + Frame stage +
Safe Mode boundary + AgentRunner. H2b moved the first three into the
Workflow bounded context:

* :class:`~backend.workflow.domain.state.LegacyWorkflowStateMachine` (+
  :class:`~backend.workflow.domain.state.LegacyWorkflowState`,
  ``LegacyStage``, :class:`~backend.workflow.domain.state.InvalidLegacyTransitionError`)
* :class:`~backend.workflow.application.stages.frame.FrameStage` (+
  :class:`~backend.workflow.application.stages.frame.FramedRequest` etc.)
* :class:`~backend.workflow.application.safe_mode.SafeModeBoundary`

What remains here — :class:`AgentRunner` — is H2c's responsibility to
relocate to :mod:`backend.workflow.application.agent_runner`. Until
then this package is a thin re-export.
"""

from __future__ import annotations

from backend.orchestrator.agent_runner import AgentRunner

__all__ = ["AgentRunner"]
