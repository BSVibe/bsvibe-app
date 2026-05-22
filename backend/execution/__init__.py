"""Execution core for the BSVibe monorepo.

Holds the pieces the live orchestration path depends on: the state
machine, advisory run-dispatch lock, verification-contract parser, and
the tool registry. The canonical orchestration schema lives in
``backend.execution.db`` (ExecutionRun-centric, payload-JSON) and is
driven by ``backend.orchestrator.agent_runner`` + the Bundle G workers.

The BSNexus orchestrator lift (orchestrator / work_steps /
run_attempt_executor / brief / directions / deliverables / verification /
run_attempts / planning / verifier.judge) was removed — the monorepo
re-implemented that surface greenfield in ``execution.db`` +
``orchestrator.agent_runner``, and the lifted modules were a dead
parallel implementation referencing a conflicting (BSNexus tenant_id)
schema. Re-lift from BSNexus if a future bundle needs that logic.

Internal domain enums (RequestStatus, WorkStepStatus, ProofState, …)
live in ``backend.execution._domain``.
"""

from backend.execution.advisory_lock import (
    advisory_key_for_run,
    release_run_dispatch_lock,
    try_run_dispatch_lock,
)
from backend.execution.state_machine import (
    can_advance_run_attempt_phase,
    can_transition_proof,
    can_transition_request,
    can_transition_work_step,
)
from backend.execution.tools import ToolDefinition, ToolError, ToolRegistry
from backend.execution.verifier.contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
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
