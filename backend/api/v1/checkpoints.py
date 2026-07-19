"""/api/v1/checkpoints — founder resolution of paused-run Decisions.

Workflow §5 #4 / §12.5 #8. When the agent loop is stuck or the work LLM
calls ``ask_user_question``, :class:`~backend.execution.orchestrator.RunOrchestrator`
mints an ``execution_decisions`` row and the run pauses (stays RUNNING — not a
DB terminal). This router is the founder's re-entry point:

* ``GET  /api/v1/checkpoints`` — list PENDING execution Decisions for the
  workspace (the blocking questions awaiting a human answer).
* ``POST /api/v1/checkpoints/{id}/resolve`` — record the founder's answer on
  the Decision, fold it into the run payload, and resume the paused run by
  transitioning it RUNNING → OPEN so :meth:`AgentWorker.drive_once` (which
  scans ``status==OPEN`` runs) re-picks it. The orchestrator then injects the
  resolved answer into the loop's initial messages so the work continues with
  the founder's decision in context.

The resolve logic itself lives in
:mod:`backend.workflow.application.checkpoint_resolution` — a shared service so
the MCP checkpoint tools can reuse it (``backend.api`` is forbidden to
``backend.mcp``). This router is a thin caller: it validates the request body,
delegates to the service, and maps the service's domain exceptions to HTTP
status codes. The list endpoints reuse the same kind → question / options /
actions helpers from :mod:`backend.workflow.application._checkpoint_shared`.

v1 resumes the run *inline* here (not via the event-driven
:class:`~backend.workflow.application.intake.decision_resolution.DecisionResolutionTrigger`, which
remains a future option). This is simpler — no phantom Request, the paused run
is resumed directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    get_current_user_row,
    get_db_session,
    get_output_language,
    get_workspace_id,
)
from backend.api.v1._workflow_deps import get_decision_repository
from backend.api.v1.decisions import _vault_root
from backend.identity.db import UserRow
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.retrieval.resolved_decisions_retriever import ResolvedDecisionsRetriever
from backend.workflow.application._checkpoint_shared import (
    DecisionAction,
    _decision_actions,
    _decision_options,
    _question_text,
)
from backend.workflow.application.checkpoint_resolution import (
    CheckpointNotFound,
    InvalidAction,
    ProductWorkspaceBusy,
    ProductWorkspaceMergeFailed,
)
from backend.workflow.application.checkpoint_resolution import (
    resolve_checkpoint as resolve_checkpoint_service,
)
from backend.workflow.domain.repositories import DecisionRepository
from backend.workflow.infrastructure.db import DecisionStatus, RunStatus

router = APIRouter()


class CheckpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    decision: str
    question: str
    # B11a: structured options the work LLM offered (``ask_user_question`` with
    # an ``options`` array). The PWA renders a single-select; the founder may
    # pick one OR write an "Other" free-text answer (L-D1). ``None`` (or empty)
    # keeps the existing pure-textarea behaviour.
    options: list[str] | None = None
    # L-D2: structured one-click actions for executor B2b Decisions
    # (``verification_failed`` / ``human_review_required``). ``None`` for
    # ask_user_question Decisions — those use ``options`` + free-text only.
    actions: list[DecisionAction] | None = None
    rationale: str | None = None
    # G4 (proposal §5.5): similar prior decisions the founder already resolved,
    # matched by question/answer/intent token overlap via the workspace's
    # ResolvedDecisionsRetriever — "Prior decision — Q: … A: …" lines so they
    # can answer consistently instead of re-deciding. Empty when nothing
    # overlaps (never fabricated); the current pending Decision can't self-match
    # because only RESOLVED decisions are absorbed into the vault.
    prior_decisions: list[str] = []
    created_at: datetime


class ResolvedCheckpointResponse(BaseModel):
    """One answered paused-run checkpoint (the Decisions "Resolved" tab,
    checkpoint side): the question + the founder's recorded answer + when."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    question: str
    resolution: str | None = None
    resolved_at: datetime | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Either ``action_key`` (L-D2 one-click) OR ``answer`` (free-text /
    # L-D1 options + Other). When both are sent, ``action_key`` wins —
    # the dispatch-driven path is the authoritative one. ``answer`` is
    # optional now (was required) because action-only resolutions don't
    # carry founder text; defaults to ``""`` and the handler resolves
    # the recorded ``decision.resolution`` from the action key instead.
    answer: str = ""
    action_key: str | None = None
    # G1: free-text "why I'm discarding this" the founder may supply alongside a
    # ``discard`` action. When present on a discard, it becomes reusable negative
    # knowledge (a ``negative_pattern`` settle); ignored for non-discard
    # resolutions. Optional — a reasonless discard simply teaches nothing.
    reason: str = ""


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    status: DecisionStatus
    resolution: str
    resolved_at: datetime
    run_status: RunStatus


def build_decisions_retriever(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> ResolvedDecisionsRetriever:
    """The caller's workspace-scoped resolved-decisions retriever (G4).

    Rooted at the same ``<vault_root>/<region>/<workspace_id>/`` storage the
    settle pipeline writes decision-resolution notes into, so a pending
    checkpoint can surface the founder's relevant prior answers. A FastAPI
    dependency so tests can inject a tmp-vault-backed retriever (mirrors
    :func:`backend.api.v1.inside.build_inside_storage`)."""
    root = _vault_root(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return ResolvedDecisionsRetriever(FileSystemStorage(root))


@router.get("")
async def list_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    decisions: Annotated[DecisionRepository, Depends(get_decision_repository)],
    retriever: Annotated[ResolvedDecisionsRetriever, Depends(build_decisions_retriever)],
    language: Annotated[str, Depends(get_output_language)],
) -> list[CheckpointResponse]:
    """List PENDING execution Decisions for the workspace, newest first.

    Each carries ``prior_decisions`` — the founder's relevant already-resolved
    decisions (G4) — so a recurring choice is answered consistently. Retrieval
    is graceful-empty + never raises, so an empty/corrupt vault degrades to no
    suggestions rather than failing the inbox.
    """
    rows = await decisions.list_pending_by_workspace(workspace_id)
    return [
        CheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            decision=row.decision,
            question=_question_text(row, language),
            options=_decision_options(row),
            actions=_decision_actions(row),
            rationale=row.rationale,
            prior_decisions=await retriever.retrieve_for_signals(_question_text(row, language)),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/resolved")
async def list_resolved_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    decisions: Annotated[DecisionRepository, Depends(get_decision_repository)],
    language: Annotated[str, Depends(get_output_language)],
) -> list[ResolvedCheckpointResponse]:
    """List RESOLVED execution Decisions for the Decisions "Resolved" tab,
    most-recently-resolved first (created_at as a stable tiebreaker)."""
    rows = await decisions.list_resolved_by_workspace(workspace_id)
    return [
        ResolvedCheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            question=_question_text(row, language),
            resolution=row.resolution,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]


@router.post("/{checkpoint_id}/resolve")
async def resolve_checkpoint(
    checkpoint_id: uuid.UUID,
    body: ResolveRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    language: Annotated[str, Depends(get_output_language)],
) -> ResolveResponse:
    """Resolve a pending Decision with the founder's answer and resume the run.

    Thin caller over
    :func:`backend.workflow.application.checkpoint_resolution.resolve_checkpoint`:
    delegates the record-answer / fold-into-run-payload / settle / audit /
    dispatch logic to the shared service and maps its domain exceptions to the
    HTTP contract — 404 (not a pending checkpoint), 400 (invalid action/answer),
    503 (product workspace busy), 500 (ship force-merge failed). The service
    owns no transaction, so this handler commits on success.
    """
    try:
        outcome = await resolve_checkpoint_service(
            session,
            workspace_id=workspace_id,
            checkpoint_id=checkpoint_id,
            answer=body.answer,
            action_key=body.action_key,
            reason=body.reason,
            actor_id=user_row.id,
            language=language,
        )
    except CheckpointNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidAction as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ProductWorkspaceBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ProductWorkspaceMergeFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    await session.commit()

    return ResolveResponse(
        id=outcome.decision_id,
        run_id=outcome.run_id,
        status=outcome.status,
        resolution=outcome.resolution,
        resolved_at=outcome.resolved_at,
        run_status=outcome.run_status,
    )
