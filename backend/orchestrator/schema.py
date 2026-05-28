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


# The path branch the frame stage classifies (Workflow §1.2 "Frame path
# branch"). B9a records the classification; B9b is the branch that ACTS on
# ``knowledge_only`` (answer from BSage, skip the loop). The agent-loop path is
# the default and behaves exactly as today.
PathClassification = Literal["knowledge_only", "agent_loop"]

# Phase 1 — the multi-stage pipeline shape. ``single`` is one run end-to-end
# (today's behaviour). ``design_then_impl`` marks a build that runs a DESIGN
# stage first (produce a spec), then has the orchestrator chain an
# IMPLEMENTATION stage that consumes it (P1-L2). Recorded on the frame; the
# orchestrator chaining + routing act on it.
PipelineKind = Literal["single", "design_then_impl"]


@dataclass
class FramedRequest:
    """Output of the ``frame`` stage — Workflow §1 stage 2."""

    skill_match: str | None
    artifact_type_hint: str | None
    # B9a — the LLM's refined natural-language intent (``None`` on the keyword
    # fallback path, which has no LLM to refine with).
    framed_intent: str | None = None
    # B9a — the path branch (Workflow §1.2). ``agent_loop`` keeps today's
    # behaviour; ``knowledge_only`` is recorded for B9b to act on.
    path_classification: PathClassification = "agent_loop"
    # P1-L2 — whether this request should run as a design→impl pipeline.
    pipeline: PipelineKind = "single"


__all__ = ["FramedRequest", "PathClassification", "PipelineKind", "Stage", "WorkflowState"]
