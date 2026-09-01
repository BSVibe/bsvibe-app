"""The production narrative generator — the injected half of the report seam.

Wraps :class:`backend.workflow.application.report_narrative.ReportNarrativeService`
so the report composition (which lives in the Workflow context, where the MCP
tools can reach it) never has to import it: the service resolves the
workspace's model account and calls an LLM, reaching ``backend.router`` /
``backend.executors`` / ``backend.connectors`` — contexts the MCP import
contract forbids that context.

Same shape as ``_retract_handler``: the *rule* is shared, only the runtime that
touches the wider system is handed in.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.application.deliverable_narrative import NarrativeGenerator


class _LlmNarrativeGenerator:
    """Builds one :class:`ReportNarrativeService` per call, over the caller's session."""

    async def narrate(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        intent: str | None,
        summary: str | None,
        diff: str | None,
        language: str,
    ) -> str | None:
        from backend.workers.emit import get_dispatch_redis_client  # noqa: PLC0415 — lazy
        from backend.workflow.application.report_narrative import (  # noqa: PLC0415 — lazy
            ReportNarrativeService,
        )

        # dispatch redis lets an EXECUTOR-account frame caller reach the worker stream
        settings = get_settings()
        service = ReportNarrativeService(
            session, settings=settings, redis=get_dispatch_redis_client(settings)
        )
        return await service.narrate(
            workspace_id=workspace_id,
            intent=intent,
            summary=summary,
            diff=diff,
            language=language,
        )


_GENERATOR = _LlmNarrativeGenerator()


def llm_narrative_generator() -> NarrativeGenerator:
    """The production :class:`NarrativeGenerator` (stateless, shared)."""
    return _GENERATOR


__all__ = ["llm_narrative_generator"]
