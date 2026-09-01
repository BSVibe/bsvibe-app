"""The deliverable report read model — one composition for REST and MCP.

The glass-box proof for one deliverable: the row itself, the
``VerificationResult`` rows recorded for its producing run (the checks BSVibe
promised, what running them returned, the verdict), the founder's original
Direction, the knowledge it consulted vs. wrote, and a plain-language
narrative.

Lives in the Workflow context so ``bsvibe_deliverables_report`` runs the SAME
composition the browser's report page runs. The one piece that cannot live
here is the narrative *generator* — it calls an LLM through the workspace's
model account, reaching ``backend.router`` / ``backend.executors`` — so it is
injected (:class:`NarrativeGenerator`), the same shape as the retract handler.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.deliverable_narrative import (
    NarrativeGenerator,
    held_delivery_item_for,
    report_narrative_for,
    split_knowledge,
)
from backend.workflow.application.deliverable_references import references_of
from backend.workflow.domain.repositories import DeliverableRepository
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    VerificationOutcome,
    VerificationResult,
)
from backend.workflow.serialization.deliverable_views import (
    DeliverableReportResponse,
    request_text_of,
    to_response,
    to_verification,
)


async def build_deliverable_report(
    *,
    deliverable_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session: AsyncSession,
    deliverables: DeliverableRepository,
    narrative_generator: NarrativeGenerator | None = None,
) -> DeliverableReportResponse | None:
    """Compose the report, or ``None`` when the deliverable is not in this
    workspace.

    ``verified`` is decided by a real PASSED ``VerificationResult`` among the
    run's rows — never inferred from the Deliverable existing. A hollow
    deliverable (none, or only failed / inconclusive) reads as needs-review,
    honestly.
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        # ``None`` rather than an HTTP error — the caller spells "unknown here"
        # (REST 404, MCP ToolError). Cross-workspace and unknown look alike, so
        # neither surface leaks existence.
        return None
    stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == row.run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.asc())
    )
    result = await session.execute(stmt)
    verifications = [to_verification(v) for v in result.scalars().all()]
    # B4 trust-integrity: the report is "verified" ONLY when a real PASSED
    # VerificationResult is among the run's recorded verifications — never
    # inferred from the Deliverable existing. A hollow deliverable (none, or only
    # failed/inconclusive) reads as needs-review, honestly.
    verified = any(v.outcome == VerificationOutcome.PASSED for v in verifications)

    # The founder's Direction that led to this work — pulled from the producing
    # run's free-form payload so the report reads request → built → checked. A
    # missing run (cleaned history) degrades to no request, never a 500.
    run = await session.get(ExecutionRun, row.run_id)
    request = (
        request_text_of(run.payload)
        if run is not None and run.workspace_id == workspace_id and isinstance(run.payload, dict)
        else None
    )

    # R1 — lazy plain-language "what this did" (cached); in ._narrative for D35.
    narrative = await report_narrative_for(
        session, row, run, request, verified, workspace_id, generator=narrative_generator
    )

    # R8 — the footer mirrors the Brief: a still-held delivery (pending Safe-Mode
    # item) gets Approve & ship / Decline; only a shipped run gets Rollback.
    run_status = run.status.value if run is not None and run.workspace_id == workspace_id else None
    held_item_id = await held_delivery_item_for(session, row.id, workspace_id)

    # R10 — keep "참고한 지식" (referenced/consulted) and "추가한 지식" (written by THIS
    # run) distinct; a run's own writes are excluded from referenced.
    referenced, written = await split_knowledge(
        session, row.run_id, workspace_id, references_of(verifications)
    )

    return DeliverableReportResponse(
        deliverable=to_response(row, verified=verified),
        request=request,
        verified=verified,
        verifications=verifications,
        run_status=run_status,
        held_delivery_item_id=held_item_id,
        references=referenced,
        written=written,
        narrative=narrative,
    )


__all__ = ["build_deliverable_report"]
