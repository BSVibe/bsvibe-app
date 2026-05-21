from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution._domain import DeliverableStatus, DeliverableType, ProofState

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Deliverable


@dataclass(frozen=True)
class WorkOutputDraft:
    project_id: uuid.UUID
    title: str
    type: DeliverableType = DeliverableType.code
    request_id: uuid.UUID | None = None
    work_step_id: uuid.UUID | None = None
    summary: str | None = None
    artifact_refs: list = field(default_factory=list)
    risk_summary: str | None = None


async def create_deliverable_from_work_output(
    *,
    tenant_id: uuid.UUID,
    draft: WorkOutputDraft,
    session: AsyncSession,
) -> Deliverable:
    deliverable = Deliverable(
        tenant_id=tenant_id,
        project_id=draft.project_id,
        request_id=draft.request_id,
        work_step_id=draft.work_step_id,
        type=draft.type,
        title=draft.title,
        summary=draft.summary,
        artifact_refs=draft.artifact_refs,
        status=DeliverableStatus.draft,
        proof_state=ProofState.verification_missing,
        risk_summary=draft.risk_summary,
    )
    session.add(deliverable)
    await session.commit()
    await session.refresh(deliverable)
    return deliverable
