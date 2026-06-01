"""/api/v1/workspace — GDPR L1 compliance endpoints.

Two endpoints land here:

* ``GET /workspace/export`` — Art. 15 (right to access) + Art. 20
  (portability). Returns a single JSON document with the caller's profile
  plus every workspace-scoped row across the key user-facing entities.
* ``GET /workspace/processing-record`` — Art. 30 (record of processing
  activities). Returns a structured doc with controller, legal basis,
  purposes, categories, recipients (incl. sub-processors), retention and
  security measures.

Both endpoints sit under ``/api/v1/workspace`` (singular) so they read as
operations against *the caller's* workspace rather than the plural
``/workspaces`` lookup router which scopes by membership across workspaces.

Layer 2 (ORM auto-filter) and layer 3 (PG RLS) both engage automatically:
:func:`backend.api.deps.get_workspace_id` publishes the contextvar AND the
RLS GUC before the route body runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.identity.db import MembershipRow, UserRow
from backend.knowledge.domain.repositories import CanonicalAnchorRepository
from backend.knowledge.infrastructure.repositories import (
    SqlAlchemyCanonicalAnchorRepository,
)
from backend.workflow.domain.repositories import RequestRepository
from backend.workflow.infrastructure.db import (
    Decision,
    Deliverable,
    ExecutionRun,
    ExecutionRunActivity,
)
from backend.workflow.infrastructure.repositories import SqlAlchemyRequestRepository
from backend.workspaces.db import ProductResourceRow, ProductRow, ResourceBindingRow, WorkspaceRow

router = APIRouter()


# ---------------------------------------------------------------------------
# Article 30 processing record — static-ish doc + dynamic workspace facts.
# ---------------------------------------------------------------------------


class SubProcessor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    purpose: str
    region: str


# The Art. 30 doc body is a const blueprint with two workspace-derived fields
# (region + legal_basis) merged in at request time. The sub-processor list
# mirrors what the PWA's /legal/sub-processors page renders — the two surfaces
# share this list so they cannot drift out of sync.
SUB_PROCESSORS: tuple[SubProcessor, ...] = (
    SubProcessor(
        name="Supabase",
        purpose="Authentication (Supabase Auth, JWKS) + Postgres database hosting.",
        region="us-east-1 / eu-west-1 (per workspace region)",
    ),
    SubProcessor(
        name="Vercel",
        purpose="PWA frontend hosting + edge delivery.",
        region="global edge (origin: iad1)",
    ),
    SubProcessor(
        name="Anthropic",
        purpose="LLM inference for the agent loop (opt-in per workspace).",
        region="us (Anthropic API)",
    ),
    SubProcessor(
        name="OpenAI",
        purpose="LLM inference for the agent loop (opt-in per workspace).",
        region="us (OpenAI API)",
    ),
)


def _processing_record(workspace: WorkspaceRow) -> dict[str, Any]:
    """Compose the Art. 30 doc for one workspace."""
    return {
        "controller": {
            "name": "BSVibe",
            "contact": "privacy@bsvibe.dev",
        },
        "workspace_id": str(workspace.id),
        "region": workspace.region,
        "legal_basis": workspace.legal_basis,
        "purposes": [
            "Operate the BSVibe AI agent OS on behalf of the workspace owner.",
            "Run the workflow state machine (intake → work → verify → ship).",
            "Persist requests, runs, deliverables and decisions for auditability.",
            "Surface a control plane (Brief / Decisions / Inside / Knowledge).",
        ],
        "categories_of_data": [
            "Identity (email, Supabase user id) — to authenticate the founder.",
            "Workspace metadata (name, region, safe-mode setting, legal basis).",
            "Operational records (runs, work-steps, deliverables, decisions).",
            "Connector bindings (third-party identifiers under founder control).",
            "Free-text payloads (founder-typed intent, rationale, notes).",
        ],
        "categories_of_recipients": [
            "Sub-processors strictly required to deliver the service.",
            "The founder themselves (owner of the workspace).",
            "Members invited by the founder (team workspaces, future).",
        ],
        "sub_processors": [sp.model_dump() for sp in SUB_PROCESSORS],
        "retention": {
            "workspaces": "Until founder requests deletion; soft-deleted rows are "
            "retained 30 days then hard-purged (Workflow §10.7).",
            "runs_and_deliverables": "Retained for the life of the workspace; "
            "exported on demand via /workspace/export.",
            "audit_events": "Retained 1 year for security incident review.",
        },
        "security_measures": [
            "Encryption in transit (TLS) and at rest (Supabase + PG TDE).",
            "Workspace isolation via 3 defense layers: request contextvar, "
            "global ORM auto-filter (with_loader_criteria), and Postgres RLS "
            "(app.current_workspace_id GUC).",
            "Soft-delete + 30-day window before hard purge.",
            "Authentication via Supabase JWT (ES256 + JWKS).",
            "RBAC on membership.role (owner > admin > editor > viewer).",
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Article 15 / 20 — export
# ---------------------------------------------------------------------------


async def _build_export(
    session: AsyncSession,
    *,
    workspace: WorkspaceRow,
    user: UserRow,
) -> dict[str, Any]:
    """Materialise the export document. All children are workspace-scoped by
    layer 2; the explicit workspace_id filters here are defense-in-depth and
    make the query intent obvious in the source.
    """
    ws_id = workspace.id

    membership = (
        await session.execute(
            select(MembershipRow).where(
                MembershipRow.user_id == user.id,
                MembershipRow.workspace_id == ws_id,
                MembershipRow.left_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    products = (
        (await session.execute(select(ProductRow).where(ProductRow.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    product_resources = (
        (
            await session.execute(
                select(ProductResourceRow).where(ProductResourceRow.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )
    bindings = (
        (
            await session.execute(
                select(ResourceBindingRow).where(ResourceBindingRow.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )

    runs = (
        (await session.execute(select(ExecutionRun).where(ExecutionRun.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    activities = (
        (
            await session.execute(
                select(ExecutionRunActivity).where(ExecutionRunActivity.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )
    deliverables = (
        (await session.execute(select(Deliverable).where(Deliverable.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    decisions = (
        (await session.execute(select(Decision).where(Decision.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    request_repo: RequestRepository = SqlAlchemyRequestRepository(session)
    requests = await request_repo.list_by_workspace(ws_id)
    anchor_repo: CanonicalAnchorRepository = SqlAlchemyCanonicalAnchorRepository(session)
    canon = await anchor_repo.list_by_workspace(ws_id)

    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "schema_version": 1,
        "profile": {
            "user_id": str(user.id),
            "supabase_user_id": user.supabase_user_id,
            "email": user.email,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "membership": (
                {
                    "id": str(membership.id),
                    "role": membership.role,
                    "joined_at": (
                        membership.joined_at.isoformat() if membership.joined_at else None
                    ),
                }
                if membership is not None
                else None
            ),
        },
        "workspace": {
            "id": str(workspace.id),
            "name": workspace.name,
            "region": workspace.region,
            "safe_mode": workspace.safe_mode,
            "legal_basis": workspace.legal_basis,
            "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
        },
        "products": [
            {
                "id": str(p.id),
                "name": p.name,
                "slug": p.slug,
                "repo_url": p.repo_url,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in products
        ],
        "product_resources": [
            {
                "id": str(pr.id),
                "product_id": str(pr.product_id),
                "kind": pr.kind,
                "title": pr.title,
                "url": pr.url,
                "note": pr.note,
            }
            for pr in product_resources
        ],
        "resource_bindings": [
            {
                "id": str(b.id),
                "product_id": str(b.product_id),
                "connector_account_id": str(b.connector_account_id),
                "resource_id": b.resource_id,
                "selection": b.selection,
                "trigger": b.trigger,
                "output_mode": b.output_mode,
            }
            for b in bindings
        ],
        "requests": [
            {
                "id": str(r.id),
                "trigger_event_id": str(r.trigger_event_id),
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in requests
        ],
        "runs": [
            {
                "id": str(run.id),
                "product_id": str(run.product_id) if run.product_id else None,
                "request_id": str(run.request_id) if run.request_id else None,
                "status": run.status.value if hasattr(run.status, "value") else str(run.status),
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "activities": [
                    {
                        "id": str(a.id),
                        "activity_type": a.activity_type,
                        "created_at": a.created_at.isoformat() if a.created_at else None,
                    }
                    for a in activities
                    if a.run_id == run.id
                ],
            }
            for run in runs
        ],
        # Artifact refs only — raw payload contents are NOT inlined (size +
        # third-party-content concerns). Founders can dereference via the
        # ``artifact_uri`` themselves.
        "deliverables": [
            {
                "id": str(d.id),
                "run_id": str(d.run_id),
                "deliverable_type": (
                    d.deliverable_type.value
                    if hasattr(d.deliverable_type, "value")
                    else str(d.deliverable_type)
                ),
                "artifact_uri": d.artifact_uri,
                "diff_url": d.diff_url,
                "retracted_at": d.retracted_at.isoformat() if d.retracted_at else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in deliverables
        ],
        "decisions": [
            {
                "id": str(dec.id),
                "run_id": str(dec.run_id),
                "decision": dec.decision,
                "rationale": dec.rationale,
                "status": (dec.status.value if hasattr(dec.status, "value") else str(dec.status)),
                "resolution": dec.resolution,
                "resolved_at": dec.resolved_at.isoformat() if dec.resolved_at else None,
                "created_at": dec.created_at.isoformat() if dec.created_at else None,
            }
            for dec in decisions
        ],
        "knowledge_concepts": [
            {
                "id": str(c.id),
                "name": c.name,
                "description": c.description,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in canon
        ],
    }


@router.get("/export")
async def export_workspace(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Return the caller's workspace data as a single JSON document.

    Art. 15 (right of access) + Art. 20 (portability). Workspace-scoped via
    the resolved ``workspace_id`` contextvar; defense layers 2/3 also engage.
    """
    workspace = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one()
    return await _build_export(session, workspace=workspace, user=user)


@router.get("/processing-record")
async def processing_record(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """Return the Art. 30 record-of-processing-activities doc for the workspace."""
    workspace = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one()
    return _processing_record(workspace)


__all__ = ["router"]
