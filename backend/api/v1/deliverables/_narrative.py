"""R1 — lazy "what this did" narrative for the deliverable report.

Kept in its own thin sub-file so :mod:`.proof` stays under the D35 250-LOC
adapter ceiling. Generates a plain-language narrative (chat model) on first
report view and caches it on the deliverable payload; verified-only,
best-effort (never breaks the read).
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun

logger = structlog.get_logger(__name__)


async def report_narrative_for(
    session: AsyncSession,
    row: Deliverable,
    run: ExecutionRun | None,
    request: str | None,
    verified: bool,
    workspace_id: uuid.UUID,
) -> str | None:
    """The cached / lazily-generated "what this did" narrative for the report."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    cached = payload.get("narrative")
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    # Only spend a generation on a verified deliverable with something to describe.
    if not verified:
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), str) else None
    diff = payload.get("diff") if isinstance(payload.get("diff"), str) else None
    intent: str | None = None
    if run is not None and isinstance(run.payload, dict):
        frame = run.payload.get("frame")
        if isinstance(frame, dict):
            intent = frame.get("framed_intent") or frame.get("summary_title")
    intent = intent or request
    if not (summary or diff or intent):
        return None
    from backend.workflow.application.report_narrative import (  # noqa: PLC0415 — lazy
        ReportNarrativeService,
    )

    service = ReportNarrativeService(session, settings=get_settings())
    narrative = await service.narrate(
        workspace_id=workspace_id, intent=intent, summary=summary, diff=diff
    )
    if not narrative:
        return None
    # Cache on the deliverable payload (re-assign so SQLAlchemy detects the JSON
    # change). Soft: a commit hiccup just means we regenerate next view.
    new_payload = dict(payload)
    new_payload["narrative"] = narrative
    row.payload = new_payload
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 — caching is best-effort, never breaks the read
        logger.warning("report_narrative_cache_failed", deliverable_id=str(row.id), exc_info=True)
    return narrative


__all__ = ["report_narrative_for"]
