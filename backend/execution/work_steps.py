from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution._domain import (
    ProofState,
    RequestStatus,
    WorkPlanCreatedBy,
    WorkPlanStatus,
    WorkStepStatus,
)
from backend.execution.state_machine import can_transition_request, can_transition_work_step

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Deliverable, Request, WorkPlan, WorkStep

logger = structlog.get_logger(__name__)


class GreenfieldStateError(ValueError):
    """Raised when a server-owned state transition is not allowed."""


@dataclass(frozen=True)
class WorkStepDraft:
    name: str
    objective: str
    expected_outputs: list[str] = field(default_factory=list)
    verifier_policy: dict | None = None


async def create_work_plan(
    *,
    request: Request,
    steps: list[WorkStepDraft],
    created_by: WorkPlanCreatedBy,
    session: AsyncSession,
) -> WorkPlan:
    if not steps:
        raise GreenfieldStateError("WorkPlan requires at least one WorkStep")

    max_version_stmt = select(func.max(WorkPlan.version)).where(WorkPlan.request_id == request.id)
    next_version = ((await session.execute(max_version_stmt)).scalar_one() or 0) + 1

    active_plans = (
        (
            await session.execute(
                select(WorkPlan)
                .where(WorkPlan.request_id == request.id, WorkPlan.status == WorkPlanStatus.active)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for active_plan in active_plans:
        active_plan.status = WorkPlanStatus.superseded

    plan = WorkPlan(
        request_id=request.id,
        version=next_version,
        steps=[_step_snapshot(step) for step in steps],
        created_by=created_by,
        status=WorkPlanStatus.active,
    )
    session.add(plan)
    await session.flush()

    created_steps: list[WorkStep] = []
    for step in steps:
        work_step = WorkStep(
            request_id=request.id,
            plan_id=plan.id,
            name=step.name,
            objective=step.objective,
            expected_outputs=step.expected_outputs,
            verifier_policy=step.verifier_policy,
        )
        session.add(work_step)
        created_steps.append(work_step)

    await session.flush()

    if request.status == RequestStatus.open:
        request.status = RequestStatus.running
    request.current_step_id = created_steps[0].id

    await session.commit()
    await session.refresh(plan)
    return plan


async def transition_work_step(
    *,
    step: WorkStep,
    target: WorkStepStatus,
    session: AsyncSession,
) -> WorkStep:
    if not can_transition_work_step(step.status, target):
        raise GreenfieldStateError(
            f"Cannot transition WorkStep from {step.status.value} to {target.value}"
        )

    step.status = target
    if target == WorkStepStatus.running:
        step.attempt_count += 1

    await session.commit()
    await session.refresh(step)
    return step


async def transition_request(
    *,
    request: Request,
    target: RequestStatus,
    session: AsyncSession,
    github_client_factory: object | None = None,
) -> Request:
    if not can_transition_request(request.status, target):
        raise GreenfieldStateError(
            f"Cannot transition Request from {request.status.value} to {target.value}"
        )

    if target == RequestStatus.shipped and not await _request_has_verified_deliverable_proof(
        session=session,
        request_id=request.id,
    ):
        raise GreenfieldStateError("Request cannot ship without verified deliverable proof")

    request.status = target
    await session.commit()
    await session.refresh(request)

    # G8.3 hook — when a Request ships and the project has a repo
    # binding, open a GitHub PR from the per-Request branch against
    # the base. Soft-fail: a PR creation error does NOT revert
    # ``shipped``, because the proof contract is the verified
    # Deliverables, not the GitHub PR. The next manual retry runs
    # cleanly because ``pr_number`` stays NULL.
    if target == RequestStatus.shipped:
        pass
        # TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.git_ops
        #         from backend.src.core.git_ops import PullRequestOpError, open_request_pr
        # TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
        #         from backend.src.models import Project

        project = await session.get(Project, request.project_id)
        if project is not None and project.github_repo_url and project.github_token_encrypted:
            try:
                info = await open_request_pr(
                    request=request,
                    session=session,
                    client_factory=github_client_factory,  # type: ignore[arg-type]
                )
                await session.commit()
                logger.info(
                    "request_pr_opened",
                    request_id=str(request.id),
                    pr_number=info.pr_number,
                    pr_url=info.pr_url,
                    created=info.created,
                )
            except PullRequestOpError as exc:
                logger.warning(
                    "request_pr_skipped",
                    request_id=str(request.id),
                    reason=exc.reason,
                    message=str(exc),
                )
    return request


def _step_snapshot(step: WorkStepDraft) -> dict:
    return {
        "name": step.name,
        "objective": step.objective,
        "expected_outputs": step.expected_outputs,
        "verifier_policy": step.verifier_policy,
    }


async def _request_has_verified_deliverable_proof(
    *, session: AsyncSession, request_id: uuid.UUID
) -> bool:
    """True when the LATEST deliverable of every WorkStep is verified.

    G-D: a retried / reframed Request accumulates the superseded failed
    deliverable alongside the new attempt on the same WorkStep. Judging
    *every* deliverable (``all(verified)``) lets a stale
    ``verification_failed`` from an earlier attempt block ``shipped``
    forever. The gate must look only at the most recent deliverable per
    WorkStep — the attempt that supersedes the rest.
    """
    deliverables = (
        (
            await session.execute(
                select(Deliverable)
                .where(Deliverable.request_id == request_id)
                .order_by(Deliverable.created_at, Deliverable.id)
            )
        )
        .scalars()
        .all()
    )
    if not deliverables:
        return False
    # Ordered oldest→newest, so the last write per WorkStep wins.
    latest_per_step: dict[uuid.UUID | None, Deliverable] = {}
    for deliverable in deliverables:
        latest_per_step[deliverable.work_step_id] = deliverable
    return all(d.proof_state == ProofState.verified for d in latest_per_step.values())
