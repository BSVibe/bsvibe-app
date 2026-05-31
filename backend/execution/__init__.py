"""Execution core for the BSVibe monorepo.

Holds the pieces the live orchestration path depends on: the
verification-contract parser, the tool registry, and the orchestration
schema (``execution.db`` — ExecutionRun-centric, payload-JSON, driven by
``backend.orchestrator.agent_runner`` + the Bundle G workers).

Lift H1 (Workflow context skeleton) relocated the state machine,
per-domain enums, and advisory lock into
:mod:`backend.workflow.domain` / :mod:`backend.workflow.infrastructure`.
This module continues to re-export the public names for back-compat
during the migration; new callers SHOULD import from the new locations.

The BSNexus orchestrator lift (orchestrator / work_steps /
run_attempt_executor / brief / directions / deliverables / verification /
run_attempts / planning / verifier.judge) was removed — the monorepo
re-implemented that surface greenfield in ``execution.db`` +
``orchestrator.agent_runner``, and the lifted modules were a dead
parallel implementation referencing a conflicting (BSNexus tenant_id)
schema. Re-lift from BSNexus if a future bundle needs that logic.
"""

from backend.execution.tools import ToolDefinition, ToolError, ToolRegistry
from backend.execution.verifier.contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)

# Lift H1 — back-compat re-exports. New code SHOULD import from the
# new locations under ``backend.workflow``.
from backend.workflow.domain.state_machine import (
    can_advance_run_attempt_phase,
    can_transition_proof,
    can_transition_request,
    can_transition_work_step,
)
from backend.workflow.infrastructure.advisory_lock import (
    advisory_key_for_run,
    release_run_dispatch_lock,
    try_run_dispatch_lock,
)

__all__ = [
    "ToolDefinition",
    "ToolError",
    "ToolRegistry",
    "VerificationCheck",
    "VerificationContract",
    "advisory_key_for_run",
    "can_advance_run_attempt_phase",
    "can_transition_proof",
    "can_transition_request",
    "can_transition_work_step",
    "parse_verification_contract",
    "release_run_dispatch_lock",
    "try_run_dispatch_lock",
]
