"""Production run orchestration (G9).

The greenfield rebuild (G0-G8) built every component of the
Direction → … → PR pipeline and proved them via the M0 benchmark
bridge + unit tests, but never wired the *autonomous production loop*.
G9 closes that:

  - Front half: ``RequestWorker`` (``workers/request_worker.py``)
    consumes ``request:queue``, calls ``plan_and_dispatch_request``.
  - Back half: ``advance_request_after_proof`` (here) is called by the
    VerifierWorker after a Deliverable's proof resolves — it walks the
    WorkStep + Request state machines forward so a fully-verified
    Request reaches ``shipped`` (which fires the G8.3 PR hook).

This module holds the pure orchestration helpers; the workers are the
thin Redis-consumer shells around them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.config
# from backend.src.config import settings as app_settings
from backend.execution._domain import (
    ProofAspectStatus,
    ProofState,
    RequestStatus,
    RunAttemptStatus,
    WorkPlanCreatedBy,
    WorkStepStatus,
)

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.executor_config.protocol
# from backend.src.core.executor_config.protocol import ExecutorClient
from backend.execution.planning import build_project_context, decompose_request
from backend.execution.run_attempt_executor import (
    _lookup_executor_config_kind_and_model,
    dispatch_run_attempt,
)

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.executor_config.resolver
# from backend.src.core.executor_config.resolver import resolve_executor
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.core.git_ops.clone
# from backend.src.core.git_ops.clone import ensure_repo_cloned
from backend.execution.run_attempts import finish_run_attempt
from backend.execution.work_steps import (
    WorkStepDraft,
    create_work_plan,
    transition_request,
    transition_work_step,
)

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import (
#     Decision,
#     Deliverable,
#     Project,
#     Request,
#     RunAttempt,
#     VerificationAspect,
#     WorkPlan,
#     WorkStep,
# )
# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models.project
# from backend.src.models.project import WorkspaceType

logger = structlog.get_logger(__name__)

_WORK_STEP_NAME_MAX = 80

# Forward-only founder Decision options. There is deliberately no
# ``abandon`` — an "abandon" option is itself a non-recourse. Every
# Decision resolution moves the work forward.
DECISION_OPTIONS: list[str] = ["retry", "reframe"]


async def create_blocking_decision(
    *,
    request: Request,
    work_step: WorkStep | None,
    reason: str,
    session: AsyncSession,
    stream_manager: object | None = None,
    detail: str | None = None,
) -> Decision:
    """Raise a blocking founder ``Decision`` for a stalled WorkStep.

    Created when work genuinely cannot auto-proceed — the continuation
    cap was reached, or a WorkStep finished ``failed`` with no recourse.
    The Request waits in ``needs_decision`` until the founder resolves
    this Decision; resolving it re-dispatches the work (``retry`` /
    ``reframe``). There is no dead-end.

    The Decision is added + flushed (not committed) — the caller owns
    the transaction boundary so the WorkStep / Request transitions land
    atomically with the Decision row.

    When ``stream_manager`` is supplied, a ``decision`` project event is
    published so the founder's Decisions view updates live instead of
    waiting for a manual refresh. Publishing is soft — a stream hiccup
    must not abort the orchestration that raised the Decision.
    """
    step_name = work_step.name if work_step is not None else "the work"
    question = (
        f'Work on "{step_name}" stalled and cannot continue on its own '
        f"(reason: {reason}). Choose how to move it forward: retry the "
        "work as-is, or reframe it with new direction."
    )
    # Carry the verifier's actual output into the Decision so the
    # founder sees WHY it stalled — a bare "stalled" verdict forces a
    # dig through logs to reframe correctly.
    if detail:
        question = f"{question}\n\nWhat the verifier saw:\n{detail.strip()}"
    decision = Decision(
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        request_id=request.id,
        work_step_id=work_step.id if work_step is not None else None,
        question=question,
        options=list(DECISION_OPTIONS),
        blocking=True,
    )
    session.add(decision)
    await session.flush()
    logger.info(
        "blocking_decision_created",
        request_id=str(request.id),
        work_step_id=str(work_step.id) if work_step is not None else None,
        decision_id=str(decision.id),
        reason=reason,
    )
    if stream_manager is not None:
        try:
            await stream_manager.publish_project_event(
                str(request.project_id),
                "decision",
                {
                    "id": str(decision.id),
                    "project_id": str(request.project_id),
                    "request_id": str(request.id),
                    "blocking": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 — soft: never abort orchestration
            logger.warning(
                "blocking_decision_event_publish_failed",
                decision_id=str(decision.id),
                error=str(exc),
            )
    return decision


_DEFAULT_AGENTS_MD = """\
# AGENTS.md

Project conventions for the AI working in this workspace. The founder
owns this file — edit it to steer how work is done here. It supplements
(does not replace) the built-in engineering rules.

## Engineering discipline

- **Test-first.** When implementing behaviour, write the test that pins
  the contract first, watch it fail, then implement until it passes.
- **One cohesive change per step.** Keep each step a commit-sized unit.
- **Green before done.** `python -m pytest`, `python -m ruff check .`,
  and `python -m ruff format --check .` must all exit 0 before you
  hand off. These are separate tools — passing one is not passing all.
- **No scratch scripts.** Don't author `run_tests.py` / `validate_*.py`
  helper files — run `pytest` directly. They are not deliverables.

## Layout

- Prefer a `src/` package layout with tests under `tests/`.
- `pyproject.toml` uses PEP 621 (`[project]`), never stdlib modules as
  dependencies.
"""


def provision_workspace(project: Project) -> Path:
    """Return the on-disk workspace directory for ``project``, creating
    it when missing.

    ``server_managed`` projects get ``<workspace_root>/<project_id>``
    lazily. ``local_import`` / ``github_connected`` projects are
    expected to carry an explicit ``workspace_dir`` already; we still
    fall back to the managed path so a run never dies on a missing
    directory. The caller persists ``project.workspace_dir`` if this
    function had to assign one.

    Also seeds a default ``AGENTS.md`` when the workspace has none —
    the founder-editable conventions file — EXCEPT for
    ``github_connected`` projects, whose cloned repo owns its own
    conventions (seeding BSNexus's Python-flavoured default there
    misdirects the work LLM on a non-Python repo).
    """
    managed = Path(app_settings.workspace_root).resolve() / str(project.id)
    if project.workspace_dir:
        path = Path(project.workspace_dir)
        # A server_managed project whose workspace_dir points OUTSIDE
        # the current workspace_root is stale — typically stamped
        # before a workspace-root / sandbox-volume migration. Keeping
        # it makes the sandbox DinD bind-mount resolve to a path it
        # cannot see. Re-provision under the managed root and re-stamp.
        # local_import / github_connected projects carry an
        # intentionally-external workspace_dir — leave those alone.
        if project.workspace_type == WorkspaceType.server_managed:
            try:
                path.resolve().relative_to(Path(app_settings.workspace_root).resolve())
            except ValueError:
                logger.info(
                    "provision_workspace_reprovisioned_stale_dir",
                    project_id=str(project.id),
                    stale_dir=str(path),
                    managed_dir=str(managed),
                )
                path = managed
                project.workspace_dir = str(managed)
    else:
        path = managed
    path.mkdir(parents=True, exist_ok=True)
    # G-B: a github_connected project's repo carries its own
    # conventions (and AGENTS.md, if any) once ``ensure_repo_cloned``
    # clones it in. Seeding BSNexus's Python-flavoured default here
    # would misdirect the work LLM into declaring a Python verification
    # contract (ruff / pytest) on a non-Python repo.
    if project.workspace_type != WorkspaceType.github_connected:
        _seed_agents_md(path)
    return path


def _seed_agents_md(workspace: Path) -> None:
    """Write a default ``AGENTS.md`` if the workspace has none. Idempotent
    and never raises — a seeding failure must not block a run."""
    agents_md = workspace / "AGENTS.md"
    if agents_md.exists():
        return
    try:
        agents_md.write_text(_DEFAULT_AGENTS_MD, encoding="utf-8")
    except OSError as exc:
        logger.warning("agents_md_seed_failed", workspace=str(workspace), error=str(exc))


async def plan_and_dispatch_request(
    *,
    request_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    stream_manager: object,
    executor: object | None = None,
    executor_kind: str | None = None,
    model: str | None = None,
) -> None:
    """Drive a freshly-created Request into execution.

    Idempotent: ``create_work_plan`` flips the Request ``open →
    running``, so a re-delivered queue message finds the Request no
    longer ``open`` and is a safe no-op. Cross-tenant ``request_id``
    raises ``LookupError`` so the worker can ack-then-skip.

    ``executor`` / ``executor_kind`` / ``model`` are passthroughs to
    ``dispatch_run_attempt`` — production leaves them ``None`` so the
    per-tenant ``ExecutorConfig`` is resolved; tests inject a stub
    executor to exercise the orchestration without a live LLM.

    G10 inserts a single decomposer LLM call between the Request
    arriving and the WorkPlan being built. Same model/auth/tool surface
    as the worker LLM, no tools, asks the model to chain-of-thought
    over the ProjectContext and emit a JSON array of WorkStepDrafts.
    When the decomposer or executor is unavailable, falls back to the
    G9 single-step plan.
    """
    stmt = select(Request).where(Request.id == request_id, Request.tenant_id == tenant_id)
    request = (await session.execute(stmt)).scalar_one_or_none()
    if request is None:
        raise LookupError(f"Request {request_id} not in tenant scope {tenant_id}")

    if request.status != RequestStatus.open:
        logger.info(
            "request_worker_skip_non_open",
            request_id=str(request_id),
            status=request.status.value,
        )
        return

    project = await session.get(Project, request.project_id)
    if project is None:
        raise LookupError(f"Project {request.project_id} not found")

    workspace_dir = provision_workspace(project)
    if not project.workspace_dir:
        project.workspace_dir = str(workspace_dir)
        await session.flush()

    # github_connected projects need the real repo in the workspace so
    # the work LLM edits actual files, not a blank tree. Soft-fail: a
    # clone error leaves the workspace as provisioned and the run still
    # proceeds (CloneResult is logged, never raised).
    clone_result = await ensure_repo_cloned(project=project, workspace_dir=workspace_dir)
    logger.info(
        "request_worker_clone",
        request_id=str(request_id),
        project_id=str(project.id),
        clone_status=clone_result.status,
    )

    intent = (request.intent or "").strip() or f"Request {request.id}"
    steps, created_by, resolved_executor, resolved_kind, resolved_model = await _build_work_steps(
        request=request,
        tenant_id=tenant_id,
        session=session,
        executor=executor,
        executor_kind=executor_kind,
        model=model,
        intent=intent,
    )
    # Hand the resolved executor down so dispatch_run_attempt doesn't
    # re-resolve per WorkStep (saves redundant DB hits + encryption work).
    executor = resolved_executor
    executor_kind = resolved_kind
    model = resolved_model

    plan = await create_work_plan(
        request=request,
        steps=steps,
        created_by=created_by,
        session=session,
    )

    work_steps = (
        (await session.execute(select(WorkStep).where(WorkStep.plan_id == plan.id))).scalars().all()
    )
    total_steps = len(work_steps)
    prior_step_names: list[str] = []
    for step_index, work_step in enumerate(work_steps):
        # ``dispatch_run_attempt`` never raises — failures are encoded
        # as ``RunAttemptStatus.failed`` + a terminal_reason string.
        # It also enqueues the resulting Deliverable on ``proof:queue``
        # so the VerifierWorker picks the loop up from here.
        result = await dispatch_run_attempt(
            request=request,
            work_step=work_step,
            tenant_id=tenant_id,
            session=session,
            step_index=step_index,
            total_steps=total_steps,
            prior_step_names=tuple(prior_step_names),
            stream_manager=stream_manager,
            workspace_dir=workspace_dir,
            executor=executor,  # type: ignore[arg-type]
            executor_kind=executor_kind,
            model=model,
        )
        logger.info(
            "request_worker_dispatched",
            request_id=str(request_id),
            work_step_id=str(work_step.id),
            terminal_reason=result.terminal_reason,
            deliverable_id=str(result.deliverable.id) if result.deliverable else None,
        )
        await session.commit()
        prior_step_names.append(work_step.name)

    # If every WorkStep failed before producing a Deliverable, no proof
    # message was enqueued — finalize the Request here so it doesn't sit
    # at ``running`` forever with nothing in flight. ``_maybe_finalize_
    # request`` routes a stuck Request to ``needs_decision`` (raising a
    # founder Decision) rather than a dead-end.
    await _maybe_finalize_request(request=request, session=session, stream_manager=stream_manager)


# Re-engagement queue contract — a re-engage message reuses the
# ``request:queue`` stream but carries ``kind=re_engage`` + the
# ``decision_id`` so the RequestWorker routes it to
# ``re_dispatch_decision`` instead of ``plan_and_dispatch_request``.
RE_ENGAGE_KIND = "re_engage"


async def re_engage_request(
    *,
    decision: Decision,
    session: AsyncSession,
    stream_manager: object,
) -> bool:
    """Move a ``needs_decision`` Request + WorkStep back to ``running``
    and enqueue a re-dispatch on ``request:queue``.

    Called from the Decision resolve handler. Enqueue-only — the actual
    ``dispatch_run_attempt`` (a multi-round LLM tool loop) runs on the
    RequestWorker, never inline in the HTTP handler.

    Returns ``True`` when a re-dispatch was enqueued, ``False`` when the
    Decision is not attached to a re-engageable Request (no request_id,
    or the Request is not ``needs_decision``) — a non-blocking,
    informational Decision, or one already re-engaged.
    """
    if decision.request_id is None:
        return False
    request = await session.get(Request, decision.request_id)
    if request is None or request.status != RequestStatus.needs_decision:
        return False

    await transition_request(request=request, target=RequestStatus.running, session=session)
    if decision.work_step_id is not None:
        work_step = await session.get(WorkStep, decision.work_step_id)
        # Re-engage ANY non-running stuck step — both ``needs_decision``
        # (executor continuation-cap park) and ``failed`` (a
        # verification-failed deliverable backstopped by
        # ``_maybe_finalize_request``). Both now have a forward edge to
        # ``running``; leaving a ``failed`` step un-transitioned would
        # crash the re-dispatch in ``_execute_one_attempt``
        # (``failed → running`` would otherwise be an illegal jump).
        if work_step is not None and work_step.status in (
            WorkStepStatus.needs_decision,
            WorkStepStatus.failed,
        ):
            await transition_work_step(
                step=work_step, target=WorkStepStatus.running, session=session
            )
    await session.commit()

    if stream_manager is None:
        logger.warning(
            "re_engage_request_not_enqueued_no_stream_manager",
            request_id=str(request.id),
            decision_id=str(decision.id),
        )
        return False

    await stream_manager.publish(
        "request:queue",
        {
            "kind": RE_ENGAGE_KIND,
            "decision_id": str(decision.id),
            "request_id": str(request.id),
            "tenant_id": str(request.tenant_id),
        },
    )
    logger.info(
        "re_engage_request_enqueued",
        request_id=str(request.id),
        decision_id=str(decision.id),
        resolution=decision.resolution,
    )
    return True


async def re_dispatch_decision(
    *,
    decision_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    stream_manager: object,
    executor: object | None = None,
    executor_kind: str | None = None,
    model: str | None = None,
) -> None:
    """Re-dispatch the WorkStep behind a resolved founder Decision.

    Runs on the RequestWorker (off the HTTP path). The Decision's
    WorkStep is driven through a fresh ``dispatch_run_attempt``. For a
    ``reframe`` resolution the founder's free-text ``guidance`` is
    seeded into a continuation handoff so ``_build_messages`` injects it
    as added direction; ``retry`` re-runs the step as-is.

    Idempotent-ish: a Decision whose Request is no longer ``running``
    (already re-finalized) is a safe no-op.
    """
    decision = (
        await session.execute(
            select(Decision).where(Decision.id == decision_id, Decision.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if decision is None:
        raise LookupError(f"Decision {decision_id} not in tenant scope {tenant_id}")
    if decision.request_id is None or decision.work_step_id is None:
        logger.info("re_dispatch_decision_skip_no_step", decision_id=str(decision_id))
        return

    request = await session.get(Request, decision.request_id)
    work_step = await session.get(WorkStep, decision.work_step_id)
    if request is None or work_step is None:
        return
    if request.status != RequestStatus.running:
        logger.info(
            "re_dispatch_decision_skip_non_running",
            decision_id=str(decision_id),
            request_status=request.status.value,
        )
        return

    project = await session.get(Project, request.project_id)
    if project is None:
        raise LookupError(f"Project {request.project_id} not found")
    workspace_dir = provision_workspace(project)

    # Recompute the step-position context the same way
    # ``plan_and_dispatch_request`` does — load the WorkStep's plan
    # steps, find this step's index, collect prior step names — so the
    # re-dispatched attempt's prompt keeps its "step N of M" + prior-step
    # framing instead of being re-run context-blind. WorkStep has no
    # explicit order column; ``create_work_plan`` inserts steps in plan
    # order and ``plan_and_dispatch_request`` reads them back the same
    # unordered way, so this mirrors the original dispatch ordering.
    plan_steps = (
        (await session.execute(select(WorkStep).where(WorkStep.plan_id == work_step.plan_id)))
        .scalars()
        .all()
    )
    total_steps = len(plan_steps) or None
    step_index: int | None = None
    prior_step_names: list[str] = []
    for idx, plan_step in enumerate(plan_steps):
        if plan_step.id == work_step.id:
            step_index = idx
            break
        prior_step_names.append(plan_step.name)

    # For ``reframe`` seed the founder's guidance into a continuation
    # handoff — ``_build_messages`` renders the handoff as a CONTINUATION
    # block, so the guidance reaches the next attempt's messages as
    # added founder direction. ``retry`` carries no handoff.
    seed_handoff: dict[str, object] | None = None
    if decision.resolution == "reframe" and (decision.guidance or "").strip():
        seed_handoff = {
            "summary": "The founder reviewed the stalled work and gave new direction.",
            "files_touched": [],
            "verification_state": "(re-engaged after a founder Decision)",
            "remaining": (decision.guidance or "").strip(),
            "blockers": "",
        }

    result = await dispatch_run_attempt(
        request=request,
        work_step=work_step,
        tenant_id=tenant_id,
        session=session,
        step_index=step_index,
        total_steps=total_steps,
        prior_step_names=tuple(prior_step_names),
        stream_manager=stream_manager,
        workspace_dir=workspace_dir,
        executor=executor,  # type: ignore[arg-type]
        executor_kind=executor_kind,
        model=model,
        seed_handoff=seed_handoff,
    )
    await session.commit()
    logger.info(
        "re_dispatch_decision_done",
        decision_id=str(decision_id),
        request_id=str(request.id),
        work_step_id=str(work_step.id),
        terminal_reason=result.terminal_reason,
    )
    await _maybe_finalize_request(request=request, session=session, stream_manager=stream_manager)


async def advance_request_after_proof(
    *,
    deliverable: Deliverable,
    session: AsyncSession,
    stream_manager: object,
) -> None:
    """Walk the WorkStep + Request state machines forward after one
    Deliverable's proof has resolved.

    Called by the VerifierWorker at the tail of ``process_one``. Soft
    by contract — the caller wraps this in try/except so an
    orchestration hiccup never reverts a verified proof.

    Transitions:
      - the Deliverable's WorkStep ``verifying`` → ``review_ready``
        (verified) or ``failed`` (verification_failed /
        human_review_required).
      - then ``_maybe_finalize_request`` checks whether every WorkStep
        on the Request is terminal and advances the Request to
        ``shipped`` (all review_ready) or ``needs_decision`` (any
        failed / needs_decision step — a founder Decision is raised).
    """
    if deliverable.work_step_id is None:
        return
    work_step = await session.get(WorkStep, deliverable.work_step_id)
    if work_step is None:
        return

    if work_step.status == WorkStepStatus.verifying:
        if deliverable.proof_state == ProofState.verified:
            await transition_work_step(
                step=work_step, target=WorkStepStatus.review_ready, session=session
            )
        elif deliverable.proof_state in (
            ProofState.verification_failed,
            ProofState.human_review_required,
        ):
            await transition_work_step(
                step=work_step, target=WorkStepStatus.failed, session=session
            )

    request = await session.get(Request, work_step.request_id)
    if request is not None:
        await _maybe_finalize_request(
            request=request, session=session, stream_manager=stream_manager
        )


async def _work_step_failure_detail(*, work_step: WorkStep, session: AsyncSession) -> str | None:
    """The verifier's actual output for a WorkStep's latest deliverable.

    Fed into the founder Decision (B) so it shows WHY the step stalled —
    a bare ``work_step_failed`` verdict forces the founder to dig
    through logs before they can reframe correctly. ``None`` when there
    is no deliverable yet (e.g. a process-lost reap) or no failed
    aspect to report.
    """
    deliverable = (
        await session.execute(
            select(Deliverable)
            .where(Deliverable.work_step_id == work_step.id)
            .order_by(Deliverable.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if deliverable is None:
        return None
    aspects = (
        (
            await session.execute(
                select(VerificationAspect).where(
                    VerificationAspect.deliverable_id == deliverable.id,
                    VerificationAspect.status != ProofAspectStatus.passed,
                )
            )
        )
        .scalars()
        .all()
    )
    if not aspects:
        return None
    lines: list[str] = []
    for aspect in aspects:
        head = f"[{aspect.aspect_type.value}] {aspect.status.value}"
        if aspect.exit_code is not None:
            head += f" (exit {aspect.exit_code})"
        lines.append(head)
        if aspect.result_summary:
            lines.append(aspect.result_summary.strip()[:600])
    return "\n".join(lines)


async def _maybe_finalize_request(
    *,
    request: Request,
    session: AsyncSession,
    stream_manager: object,
) -> None:
    """Advance the Request once all its (active-plan) WorkSteps are
    terminal. No-op while any step is still pending/running/verifying.

    All steps ``review_ready`` → ``running → review_ready → shipped``.
    The ``shipped`` transition fires the G8.3 PR hook inside
    ``transition_request``. Any step ``failed`` → for each such step
    without an open Decision a blocking founder Decision is raised, and
    the Request moves ``running → needs_decision``. There is no
    ``blocked`` dead-end — the founder always has recourse.

    A step ``needs_decision`` is itself terminal-for-finalization: the
    executor continuation cap already raised its Decision, so this only
    needs to move the Request to ``needs_decision``.
    """
    if request.status != RequestStatus.running:
        return

    active_plan = (
        await session.execute(
            select(WorkPlan)
            .where(WorkPlan.request_id == request.id)
            .order_by(WorkPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_plan is None:
        return

    steps = (
        (await session.execute(select(WorkStep).where(WorkStep.plan_id == active_plan.id)))
        .scalars()
        .all()
    )
    if not steps:
        return

    terminal = {
        WorkStepStatus.review_ready,
        WorkStepStatus.failed,
        WorkStepStatus.skipped,
        # ``needs_decision`` is terminal-for-finalization: the executor
        # continuation cap already parked the step here and raised its
        # Decision. The Request must not sit at ``running`` forever.
        WorkStepStatus.needs_decision,
    }
    if any(step.status not in terminal for step in steps):
        return  # still work in flight

    stuck_steps = [
        step
        for step in steps
        if step.status in (WorkStepStatus.failed, WorkStepStatus.needs_decision)
    ]
    all_ready = all(step.status == WorkStepStatus.review_ready for step in steps)

    if stuck_steps:
        # No dead-end: route to ``needs_decision``. The executor
        # continuation cap already raised a Decision for any step it
        # parked at ``needs_decision``; here we backstop the ``failed``
        # steps that have no open Decision yet (e.g. an executor error
        # or an ``executor_unconfigured`` finish).
        open_decision_step_ids = set(
            (
                await session.execute(
                    select(Decision.work_step_id).where(
                        Decision.request_id == request.id,
                        Decision.resolved_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for step in stuck_steps:
            if step.id not in open_decision_step_ids:
                await create_blocking_decision(
                    request=request,
                    work_step=step,
                    reason=f"work_step_{step.status.value}",
                    session=session,
                    stream_manager=stream_manager,
                    detail=await _work_step_failure_detail(work_step=step, session=session),
                )
        await transition_request(
            request=request, target=RequestStatus.needs_decision, session=session
        )
        logger.info("request_finalized_needs_decision", request_id=str(request.id))
        return

    if all_ready:
        await transition_request(
            request=request, target=RequestStatus.review_ready, session=session
        )
        # ``shipped`` is gated on every Deliverable being verified
        # (``_request_has_verified_deliverable_proof``) and fires the
        # G8.3 PR hook. ``transition_request`` raises GreenfieldStateError
        # if the gate fails — let the caller's soft-fail wrapper log it.
        await transition_request(
            request=request,
            target=RequestStatus.shipped,
            session=session,
            github_client_factory=None,
        )
        logger.info("request_finalized_shipped", request_id=str(request.id))


async def reap_orphaned_run_attempts(
    *,
    session: AsyncSession,
    stream_manager: object | None = None,
) -> int:
    """Fail RunAttempts left ``running`` by a dead process (G-A).

    A work phase runs in-process. If the process dies mid-phase
    (container recreate, crash) the RunAttempt is never finished and
    sits ``running`` forever — a zombie no founder Decision can reach
    (PR #181's no-zombie fix only covers in-process raise sites).

    Run once at startup: a fresh process is booting, so every
    ``running`` RunAttempt is by definition orphaned. Each is failed
    (``process_lost``), its WorkStep failed, and the Request routed to
    ``needs_decision`` with a founder Decision (retry / reframe) — the
    same forward-only recourse a normal work failure gets. Per-attempt
    failures are swallowed so one bad row can't block the rest of the
    sweep. Returns the number of attempts reaped.
    """
    orphaned = (
        (
            await session.execute(
                select(RunAttempt).where(RunAttempt.status == RunAttemptStatus.running)
            )
        )
        .scalars()
        .all()
    )
    reaped = 0
    for attempt in orphaned:
        try:
            await finish_run_attempt(
                attempt=attempt,
                status=RunAttemptStatus.failed,
                terminal_reason="process_lost",
                session=session,
            )
            work_step = await session.get(WorkStep, attempt.work_step_id)
            if work_step is not None and work_step.status in (
                WorkStepStatus.running,
                WorkStepStatus.verifying,
            ):
                await transition_work_step(
                    step=work_step, target=WorkStepStatus.failed, session=session
                )
            if work_step is not None:
                request = await session.get(Request, work_step.request_id)
                if request is not None:
                    await _maybe_finalize_request(
                        request=request, session=session, stream_manager=stream_manager
                    )
            reaped += 1
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            logger.exception("reap_orphaned_run_attempt_failed", run_attempt_id=str(attempt.id))
    if reaped:
        logger.info("reaped_orphaned_run_attempts", count=reaped)
    return reaped


async def _build_work_steps(
    *,
    request: Request,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    executor: ExecutorClient | None,
    executor_kind: str | None,
    model: str | None,
    intent: str,
) -> tuple[list[WorkStepDraft], WorkPlanCreatedBy, ExecutorClient | None, str | None, str | None]:
    """Resolve the WorkPlan's step list for ``request``.

    Tries the CoT decomposer first when an executor is available. Falls
    back to the G9 single-step plan when:
      - no per-tenant ExecutorConfig exists (decomposer would not have
        a model to call anyway — leave that for dispatch_run_attempt to
        record as ``executor_unconfigured``);
      - the decomposer returns no usable steps (handled inside
        ``decompose_request`` — it already emits a single-step fallback,
        so this path is just structural defense).

    Returns ``(steps, created_by, executor, executor_kind, model)`` so
    the caller can reuse the resolved executor for ``dispatch_run_attempt``.
    """
    if executor is None:
        kind, resolved_model = await _lookup_executor_config_kind_and_model(
            tenant_id=tenant_id, session=session
        )
        if kind is not None:
            resolved = await resolve_executor(tenant_id=tenant_id, session=session)
            if resolved is not None:
                executor = resolved
                executor_kind = kind
                model = resolved_model

    if executor is None:
        # No executor configured for this tenant. Build a single-step
        # plan so dispatch_run_attempt can attribute the failure as
        # ``executor_unconfigured``.
        step_name = intent.splitlines()[0][:_WORK_STEP_NAME_MAX]
        return (
            [WorkStepDraft(name=step_name, objective=intent, expected_outputs=[])],
            WorkPlanCreatedBy.system,
            executor,
            executor_kind,
            model,
        )

    ctx = await build_project_context(request=request, session=session)
    # Wire-required metadata. The decomposer fires BEFORE any
    # RunAttempt is created, so ``run_id`` is a synthetic per-Request
    # placeholder. BSGateway accepts it; DirectLLMAdapter just needs
    # both fields truthy.
    decomposer_metadata = {
        "tenant_id": str(tenant_id),
        "run_id": f"decompose:{request.id}",
        "request_id": str(request.id),
        "project_id": str(request.project_id),
    }
    steps = await decompose_request(
        ctx,
        executor=executor,
        model=model or "",
        metadata=decomposer_metadata,
    )
    return steps, WorkPlanCreatedBy.llm_assisted, executor, executor_kind, model
