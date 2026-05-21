from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution._domain import (
    BriefScope,
    DeliverableStatus,
    ProofAspectStatus,
    ProofAspectType,
    ProofState,
    RequestStatus,
)

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.git_ops
# from backend.src.core.git_ops import build_deliverable_diff_url
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Decision, Deliverable, Project, Request, VerificationAspect


def _empty_sections() -> dict:
    return {
        "shipped": [],
        "needs_decision": [],
        "blocked": [],
        "running": [],
        "next": [],
    }


def empty_brief_snapshot(project_id: uuid.UUID | None = None) -> dict:
    return {
        "scope": BriefScope.project.value if project_id is not None else BriefScope.company.value,
        "project_id": project_id,
        "sections": _empty_sections(),
        "generated_at": datetime.now(timezone.utc),
    }


async def build_brief_snapshot(
    *,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    limit_per_section: int = 10,
) -> dict:
    """Compose the typed Brief snapshot.

    Wire shape — :class:`backend.src.schemas.greenfield.BriefSnapshotResponse` —
    is strongly typed per section. ``shipped`` carries verified deliverables
    only; the ``blocked`` section carries deliverables whose proof
    failed/missing. A stalled Request waits in ``needs_decision`` and
    surfaces there via its open founder Decision.

    ``next`` is reserved for AI-recommended directions; it stays empty
    until that surface lands (file-disposition.md REVIEW_LATER).
    """
    sections = {
        "shipped": await _shipped_cards(session, tenant_id, project_id, limit_per_section),
        "needs_decision": await _decision_cards(session, tenant_id, project_id, limit_per_section),
        "blocked": await _blocked_cards(session, tenant_id, project_id, limit_per_section),
        "running": await _request_cards(
            session, tenant_id, project_id, RequestStatus.running, limit_per_section
        ),
        "next": [],
    }
    return {
        "scope": BriefScope.project.value if project_id is not None else BriefScope.company.value,
        "project_id": project_id,
        "sections": sections,
        "generated_at": datetime.now(timezone.utc),
    }


async def _shipped_cards(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[dict]:
    # ``proof_state == verified`` IS the "shipped" signal — the brief
    # section's contract is "verifier passed → it appears here". A
    # passing deliverable is stamped ``review_ready``; nothing advances
    # it to ``shipped`` (server_managed projects have no PR-merge step),
    # so gating on ``status == shipped`` left this section permanently
    # empty. Gate on proof_state; accept review_ready + shipped, which
    # also excludes founder-``rejected`` deliverables.
    stmt = (
        select(Deliverable)
        .where(
            Deliverable.tenant_id == tenant_id,
            Deliverable.proof_state == ProofState.verified,
            Deliverable.status.in_([DeliverableStatus.review_ready, DeliverableStatus.shipped]),
        )
        .order_by(Deliverable.updated_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(Deliverable.project_id == project_id)
    deliverables = (await session.execute(stmt)).scalars().all()
    return [await _deliverable_card(session, deliverable) for deliverable in deliverables]


async def _decision_cards(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[dict]:
    stmt = (
        select(Decision)
        .where(Decision.tenant_id == tenant_id, Decision.resolved_at.is_(None))
        .order_by(Decision.blocking.desc(), Decision.created_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(Decision.project_id == project_id)
    decisions = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(decision.id),
            "project_id": str(decision.project_id),
            "question": decision.question,
            "blocking": decision.blocking,
            "created_at": decision.created_at,
        }
        for decision in decisions
    ]


async def _blocked_cards(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[dict]:
    """Deliverables with failed / missing proof.

    The ``RequestStatus.blocked`` dead-end is retired — a stalled
    Request now waits in ``needs_decision`` and already surfaces in the
    ``needs_decision`` section via its open founder Decision (see
    ``_decision_cards``). So this section is now deliverable-only;
    every card carries ``kind == "deliverable"`` for the frontend's
    discriminated union.
    """
    stmt = (
        select(Deliverable)
        .where(
            Deliverable.tenant_id == tenant_id,
            Deliverable.proof_state.in_(
                [
                    ProofState.verification_failed,
                    ProofState.human_review_required,
                    ProofState.verification_missing,
                ]
            ),
            Deliverable.status == DeliverableStatus.shipped,
        )
        .order_by(Deliverable.updated_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(Deliverable.project_id == project_id)
    deliverables = (await session.execute(stmt)).scalars().all()
    blocked: list[dict] = []
    for deliverable in deliverables:
        card = await _deliverable_card(session, deliverable)
        blocked.append({**card, "kind": "deliverable"})
    return blocked


async def _request_cards(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    status: RequestStatus,
    limit: int,
) -> list[dict]:
    stmt = (
        select(Request)
        .where(Request.tenant_id == tenant_id, Request.status == status)
        .order_by(Request.updated_at.desc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(Request.project_id == project_id)
    requests = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(request.id),
            "project_id": str(request.project_id),
            "intent": request.intent,
            "status": request.status.value,
            "created_at": request.created_at,
            "updated_at": request.updated_at,
            "pr_number": request.pr_number,
            "pr_url": request.pr_url,
        }
        for request in requests
    ]


async def _latest_test_aspect(
    session: AsyncSession, deliverable_id: uuid.UUID
) -> VerificationAspect | None:
    """Return the most recent ``code_test`` aspect for a deliverable.

    The Brief card surfaces the test aspect as the primary verifier
    signal — that's the row whose ``result_summary`` / ``completed_at``
    join into the card. Lint / install_smoke aspects also contribute
    to ``proof_state`` (via the roll-up) but the card stays focused on
    "did pytest pass". A richer multi-aspect card is a later milestone.
    """
    stmt = (
        select(VerificationAspect)
        .where(
            VerificationAspect.deliverable_id == deliverable_id,
            VerificationAspect.aspect_type == ProofAspectType.code_test,
        )
        .order_by(VerificationAspect.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _deliverable_card(session: AsyncSession, deliverable: Deliverable) -> dict:
    aspect = await _latest_test_aspect(session, deliverable.id)
    verified_at: datetime | None = None
    if aspect is not None and aspect.status == ProofAspectStatus.passed:
        verified_at = aspect.completed_at
    project = await session.get(Project, deliverable.project_id)
    diff_url = (
        build_deliverable_diff_url(project=project, deliverable=deliverable) if project else None
    )
    return {
        "id": str(deliverable.id),
        "project_id": str(deliverable.project_id),
        "request_id": str(deliverable.request_id) if deliverable.request_id else None,
        "title": deliverable.title,
        "type": deliverable.type.value,
        "proof_state": deliverable.proof_state.value,
        "proof_summary": aspect.result_summary if aspect is not None else None,
        "verifier_type": aspect.aspect_type.value if aspect is not None else None,
        "verified_at": verified_at,
        "created_at": deliverable.created_at,
        "artifact_refs": list(deliverable.artifact_refs or []),
        "commit_sha": deliverable.commit_sha,
        "diff_url": diff_url,
    }
