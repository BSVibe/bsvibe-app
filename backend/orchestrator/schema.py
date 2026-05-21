"""Orchestrator schemas — Workflow §1 (3+ε state machine).

Workflow §12.5 #8 (Bundle G — Orchestrator). The 3+ε stages are:

* ``receive`` — intake landed, ready to frame
* ``frame`` — derive skill/artifact-type hints
* ``agent_loop`` — execution layer drives Work/Verify/Settle
* ``epsilon`` — terminal cleanup / post-settle compensations
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)


Stage = Literal["receive", "frame", "agent_loop", "epsilon"]


@dataclass
class WorkflowState:
    """Current workflow position for one Request — Workflow §1."""

    stage: Stage
    request_id: uuid.UUID
    run_id: uuid.UUID | None = None


@dataclass
class FramedRequest:
    """Output of the ``frame`` stage — Workflow §1 stage 2."""

    skill_match: str | None
    artifact_type_hint: str | None


__all__ = ["FramedRequest", "Stage", "WorkflowState"]
