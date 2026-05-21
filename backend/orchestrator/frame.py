"""FrameStage — derive skill/artifact-type hints from a raw Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The frame stage is the
2nd stage of the 3+ε state machine (Workflow §1). It looks at the
trigger payload + workspace skill registry and decides which skill
(if any) should handle the request, plus a hint about what artifact
type the deliverable will be.
"""

from __future__ import annotations

import structlog

from backend.intake.db import RequestRow
from backend.orchestrator.schema import FramedRequest

logger = structlog.get_logger(__name__)


class FrameStage:
    """Convert a raw Request into a framed plan."""

    async def frame(self, *, request: RequestRow) -> FramedRequest:
        """Inspect the request and return framing hints."""
        # TODO(bundle-g-integration): call SkillLoader.list_for_workspace
        # then run a small classifier (Workflow §6 #5 retrieval-prime
        # against skill summaries). Lift target: BSNexus
        # backend/execution/framing.py.
        logger.debug(
            "frame_stage_stub",
            request_id=str(request.id),
            workspace_id=str(request.workspace_id),
        )
        raise NotImplementedError("FrameStage.frame pending Bundle G integration")


__all__ = ["FrameStage"]
