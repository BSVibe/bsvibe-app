"""BSNexus execution core lifted into the BSVibe monorepo.

This bundle holds the state machine, orchestrator, run-attempt executor,
verifier, planning decomposer, and tool registry that drive a Request
through its lifecycle (open → running → review_ready → shipped).

Imports that crossed Bundle boundaries (gateway dispatch, delivery git
ops, intake workspace seeding, supervisor sandbox, etc.) are marked
with ``# TODO(bundle-x-integration):`` comments at their original call
sites and will be wired during Bundle G/X integration.

Internal domain enums (RequestStatus, WorkStepStatus, ProofState, …)
live in ``backend.execution._domain`` — lifted from BSNexus
``core/domain.py`` since they are execution-internal runtime values,
not Bundle G shared schemas.
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
