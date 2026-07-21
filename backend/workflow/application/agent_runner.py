"""AgentRunner — drive the execution layer for one Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The agent runner is the
bridge between the workflow state machine and the execution layer
(Bundle X). It opens an ExecutionRun row, advances it through the
``open → running → review_ready → shipped`` lifecycle, and surfaces
the resulting run_id.

It opens the run, then delegates the compute loop to
:class:`backend.workflow.application.agent_loop.RunOrchestrator` and
maps the loop's terminal outcome back onto the run status:

* ``verified`` → ``review_ready`` (work done, awaiting ship/delivery).
* ``needs_decision`` → run stays ``running`` (paused on a Decision row;
  resolution re-enters the loop — not a DB terminal).
* ``system_error`` → ``failed``.

This module owns the *transactional* lifecycle; ``RunOrchestrator`` owns
the *compute* lifecycle.

Lift M2 (v8 §20.3 Pattern B audit, 2026-06-02) — **legitimate coordinator,
skipped.** AgentRunner owns one cohesive responsibility: transactional
ExecutionRun lifecycle (open, transition, post-transition reactions —
auto-ship at REVIEW_READY for product-bound runs, design→impl handoff
spawning). Persistence is delegated to ``RunRepository`` /
``DeliverableRepository`` / ``RunRoutingRuleRepository`` (Lift I-Repo
seam). The post-transition reactions (``_auto_ship_product_run``,
``_maybe_spawn_impl_run``) are tightly coupled to the transition that
triggers them — extracting as policy strategies would force the caller
to re-derive trigger conditions externally, harming the invariant that
status transitions atomically advance the run.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.identity.workspaces_db import load_workspace_language
from backend.notifications.copy import notification_copy
from backend.notifications.emit import emit_notification
from backend.router.domain.repositories import RunRoutingRuleRepository
from backend.router.infrastructure.repositories import SqlAlchemyRunRoutingRuleRepository
from backend.workflow.application.agent_loop import LoopResult, RunCompute
from backend.workflow.application.handoff import capture_design_spec_text
from backend.workflow.domain.repositories import DeliverableRepository, RunRepository
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.intake.db import RequestRow
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
)

logger = structlog.get_logger(__name__)


# A model executor (a coding-agent CLI) can report a *billing* failure — the
# provider being out of credits — as a generic ``401 Invalid authentication
# credentials``. We can't tell auth from billing apart from the CLI's string
# (the mistranslation happens inside the CLI, before we see it), so when a run
# fails with an auth-shaped reason we APPEND a hint pointing at the provider
# balance. Reasons that don't look auth-related are left untouched.
_AUTH_FAILURE_RE = re.compile(
    r"\b(401|unauthorized|authenticate|authentication credentials)\b", re.IGNORECASE
)
_CREDITS_HINT = (
    " — Hint: an auth error from a model executor often means the model provider is "
    "OUT OF CREDITS / BALANCE (some agent CLIs report a billing failure as a 401). "
    "Verify your provider's balance, not just the API key."
)


def _with_credits_hint(reason: str) -> str:
    """Append the credits/billing hint to an auth-shaped failure reason.

    Idempotent (won't double-append) and a no-op for empty or non-auth reasons.
    """
    if not reason or _CREDITS_HINT in reason or not _AUTH_FAILURE_RE.search(reason):
        return reason
    return reason + _CREDITS_HINT


def _is_linked_worktree(worktree: Path) -> bool:
    """True iff ``worktree`` is a LINKED git worktree — i.e. ``.git`` is the
    gitdir-pointer FILE that shares the product repo's ref store.

    Only a linked worktree's run branch is visible to the product repo, so only
    it can be auto-shipped via the local ``merge_to_main`` fast-forward. A
    github-binding-provisioned run is a STANDALONE CLONE (``.git`` is a
    DIRECTORY with its own ref store + ``origin`` remote); its branch is
    invisible to the product repo and ``merge_to_main`` can only fail there —
    those runs deliver via the push+PR path instead (issue #362). A run with no
    ``.git`` at all (glue tests bypassing the provisioner) is also not linked.
    """
    return (worktree / ".git").is_file()


class AgentRunner:
    """Spawn + supervise one ExecutionRun for a Request."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        run_repository: RunRepository | None = None,
        deliverable_repository: DeliverableRepository | None = None,
        run_routing_rule_repository: RunRoutingRuleRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        # Settings power the design→impl handoff's spawn-time spec capture
        # (product / run workspace roots). Defaulted so production callers need
        # not thread it; tests inject one pointing at a tmp root.
        self._settings = settings or get_settings()
        # Repository constructed from the session by default — the Lift
        # I-Repo-Workflow seam. Tests may inject a fake; production callers
        # rely on the default ``SqlAlchemyRunRepository(session)``.
        self._runs: RunRepository = run_repository or SqlAlchemyRunRepository(session)
        # Lift I-Repo-Workflow-2 — the spec-handoff path reads the design run's
        # Deliverable(s) via this Repository instead of a raw select().
        self._deliverables: DeliverableRepository = (
            deliverable_repository or SqlAlchemyDeliverableRepository(session)
        )
        # Lift I-Repo-Router — the design→impl handoff gate consults the
        # Router context's rule set via Protocol instead of issuing a raw
        # SQLAlchemy query. Workflow → Router cross-context dependency
        # travels through the Protocol seam.
        self._run_routing_rules: RunRoutingRuleRepository = (
            run_routing_rule_repository or SqlAlchemyRunRoutingRuleRepository(session)
        )

    async def open_run(self, *, request: RequestRow) -> uuid.UUID:
        """Mint an ExecutionRun row tied to ``request``; returns run_id.

        Idempotent: if a run for this request already exists, returns its
        id without creating a duplicate.
        """
        existing = await self._runs.find_by_request_id(request.id)
        if existing is not None:
            return existing.id

        # D3: propagate the triggering Resource binding onto the run payload so
        # the DeliveryWorker can key the per-Run Safe Mode gate off the binding's
        # ``output_mode`` (Synthesis §11 / Workflow §10.5). The Receive stage
        # writes ``binding_id`` onto the Request payload for connector-inbound
        # triggers (a founder-direct / unbound run simply has none, which falls
        # back to the workspace-flag behavior). Forwarding it here is the single
        # point where a Run "learns" its triggering Resource.
        run_payload: dict[str, object] = {"request_id": str(request.id)}
        req_payload = request.payload if isinstance(request.payload, dict) else {}
        binding_id = req_payload.get("binding_id")
        if isinstance(binding_id, str) and binding_id:
            run_payload["binding_id"] = binding_id
        # PT3: propagate the trigger ``kind`` (e.g. ``product_tick``) onto the run
        # payload via the SAME seam as ``binding_id`` — the DeliveryWorker reads
        # it to force Safe Mode for autonomous tick-origin deliverables.
        kind = req_payload.get("kind")
        if isinstance(kind, str) and kind:
            run_payload["kind"] = kind

        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=request.workspace_id,
            # L-P1: propagate product_id from the Request (the Request copies
            # it from the TriggerEvent during intake). The previous hardcoded
            # ``None`` is what dropped product binding on every run, so e.g.
            # founder-direct submits never showed up on a product detail page.
            product_id=request.product_id,
            request_id=request.id,
            status=RunStatus.OPEN,
            payload=run_payload,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self._session.add(run)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=request.workspace_id,
                from_status=None,
                to_status=RunStatus.OPEN,
                reason="opened by agent_runner",
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_opened",
            request_id=str(request.id),
            run_id=str(run.id),
        )
        return run.id

    async def drive(
        self,
        *,
        run_id: uuid.UUID,
        orchestrator: RunCompute,
        workspace_dir: Path,
    ) -> LoopResult:
        """Run the compute loop for ``run_id`` and reconcile its outcome
        with the transactional run status.

        ``orchestrator`` is any :class:`RunCompute` — the native
        :class:`~backend.workflow.application.agent_loop.RunOrchestrator`
        or the
        :class:`~backend.workflow.application.knowledge_orchestrator.KnowledgeAnswerOrchestrator`.
        Both have the same ``run(...) -> LoopResult``
        shape, so the outcome mapping below is backend-agnostic.

        Transitions ``open → running`` before the loop, then maps the
        terminal outcome: ``verified → review_ready``, ``system_error →
        failed``, ``needs_decision`` leaves the run ``running`` (paused).
        """
        run = await self._runs.get(run_id)
        if run is None:
            raise ValueError(f"ExecutionRun {run_id} not found")

        await self.transition(
            run_id=run_id, to_status=RunStatus.RUNNING, reason="agent loop started"
        )
        result = await orchestrator.run(run=run, workspace_dir=workspace_dir)

        if result.outcome == "verified":
            await self.transition(
                run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="agent loop verified"
            )
        elif result.outcome == "system_error":
            await self.transition(
                run_id=run_id,
                to_status=RunStatus.FAILED,
                reason=_with_credits_hint(result.summary or "agent loop system error"),
            )
        # needs_decision: run stays RUNNING (paused on a Decision row).
        logger.info(
            "agent_runner_loop_complete",
            run_id=str(run_id),
            outcome=result.outcome,
        )
        return result

    async def transition(
        self,
        *,
        run_id: uuid.UUID,
        to_status: RunStatus,
        reason: str | None = None,
    ) -> bool:
        """Append history + flip ExecutionRun.status. Returns False on no-op.

        W2: when transitioning to ``REVIEW_READY`` on a product-bound run,
        auto-ship — fast-forward main onto the run branch (under advisory
        lock) and cascade to SHIPPED. Non-product runs (Direct-path / no
        product binding) transition to REVIEW_READY and stay there
        unchanged, matching pre-W1 behavior for tests + legacy code paths.
        """
        run = await self._runs.get(run_id)
        if run is None:
            return False
        if run.status is to_status:
            return False
        # L9 — cooperative cancel: CANCELLED is terminal. Once the founder
        # cancels a run, the worker's in-flight drive must NOT flip it back
        # (→ RUNNING at start, → REVIEW_READY / FAILED at the end) — those
        # transitions no-op. The ONLY allowed exit is the explicit retry
        # (CANCELLED → OPEN, which re-opens the run for another attempt).
        if run.status is RunStatus.CANCELLED and to_status is not RunStatus.OPEN:
            return False
        from_status = run.status
        run.status = to_status
        run.updated_at = datetime.now(tz=UTC)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=run.workspace_id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_transitioned",
            run_id=str(run_id),
            from_status=from_status.value,
            to_status=to_status.value,
        )

        # Notifier N3 — the SINGLE run-terminal-FAILED funnel (both production
        # FAILED writes route through this transition). Queue a ``failed``
        # notification in THIS transaction; the ``run.status is to_status``
        # guard above already no-ops a repeat, so the founder is told once.
        if to_status is RunStatus.FAILED:
            await self._emit_run_failed(run, reason)

        # W2 — auto-ship on REVIEW_READY for product-bound runs that
        # actually have a git worktree on disk. Glue tests that bypass
        # the workspace provisioner (no worktree) skip auto-ship and
        # leave the run at REVIEW_READY — exactly the pre-W2 invariant.
        if to_status is RunStatus.REVIEW_READY and run.product_id is not None:
            from backend.storage.product_workspace import (  # noqa: PLC0415
                run_worktree_path,
            )

            if _is_linked_worktree(run_worktree_path(run.id)):
                await self._auto_ship_product_run(run)

        # P1-L2 — design→impl handoff. When a DESIGN-stage run in a
        # ``design_then_impl`` pipeline reaches its verified terminal, spawn the
        # IMPLEMENTATION run (seeded with the design run's id + produced refs).
        # Gated on stage != "impl" so the impl run never re-spawns itself.
        if to_status is RunStatus.REVIEW_READY:
            await self._maybe_spawn_impl_run(run)
        return True

    async def _emit_run_failed(self, run: ExecutionRun, reason: str | None) -> None:
        """Queue the ``failed`` notification for a run that reached its FAILED
        terminal. The push ``title``/``body`` are rendered by the localized copy
        catalog in the workspace's ``workspaces.language`` (KO/EN) — the honest
        transition ``reason`` (e.g. a frame-unclassified or system error) rides
        through as the verbatim ``detail``, deep-linked to the run. Deduped on
        ``failed:<run_id>``."""
        language = await load_workspace_language(self._session, run.workspace_id)
        copy = notification_copy("failed", language, detail=(reason or "").strip())
        await emit_notification(
            self._session,
            workspace_id=run.workspace_id,
            event="failed",
            dedupe_key=f"failed:{run.id}",
            payload={
                "title": copy.title,
                "body": copy.body,
                "link": f"/runs/{run.id}",
                "run_id": str(run.id),
            },
            producer_id="workflow:run_failed",
        )

    async def _auto_ship_product_run(self, run: ExecutionRun) -> None:
        """Fast-forward main onto the run branch and transition to SHIPPED.

        Pre-conditions: ``verify`` already ran ``commit_worktree`` +
        ``merge_main_into_worktree`` (cleanly) before transitioning to
        REVIEW_READY, so the run's branch is a strict descendant of main.
        The advisory lock here protects the ``merge_to_main``
        fast-forward from a parallel ship moving main between the
        verify-time merge and this call.

        Failure modes:

        * Lock busy → leave run at REVIEW_READY; next AgentWorker tick
          (or a follow-up trigger) retries. Doesn't block.
        * Fast-forward refused (rare — verify-time merge stale) → leave
          run at REVIEW_READY with a history note. The next verify
          round will pull main again.
        * Worktree cleanup fails → logged, run still ships (cleanup is
          best-effort; the next worker tick retries).
        """
        from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
            ProductWorkspaceBusy,
            ProductWorkspaceError,
            merge_to_main,
            product_workspace_lock,
            remove_run_worktree,
        )

        product_id = run.product_id
        if product_id is None:
            return

        try:
            async with product_workspace_lock(self._session, product_id):
                sha = await merge_to_main(product_id, run.id)
                logger.info(
                    "auto_ship_merge_to_main",
                    run_id=str(run.id),
                    product_id=str(product_id),
                    main_sha=sha,
                )
            # Transition past REVIEW_READY → SHIPPED. The history row
            # is recorded directly here rather than re-calling
            # ``transition`` (which would recurse).
            run.status = RunStatus.SHIPPED
            run.updated_at = datetime.now(tz=UTC)
            self._session.add(
                ExecutionRunHistory(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    workspace_id=run.workspace_id,
                    from_status=RunStatus.REVIEW_READY,
                    to_status=RunStatus.SHIPPED,
                    reason="auto-shipped after verify",
                    created_at=datetime.now(tz=UTC),
                )
            )
            await self._session.flush()
            # Best-effort worktree cleanup (idempotent — covers retries).
            try:
                await remove_run_worktree(product_id, run.id)
            except ProductWorkspaceError:
                logger.warning(
                    "auto_ship_worktree_cleanup_failed",
                    run_id=str(run.id),
                    exc_info=True,
                )
        except ProductWorkspaceBusy:
            logger.info(
                "auto_ship_lock_busy",
                run_id=str(run.id),
                product_id=str(product_id),
            )
            # Leave at REVIEW_READY; next tick retries.
        except ProductWorkspaceError:
            logger.warning(
                "auto_ship_merge_failed",
                run_id=str(run.id),
                product_id=str(product_id),
                exc_info=True,
            )
            # Leave at REVIEW_READY; next verify round will pull main
            # again and either succeed or surface a conflict.

    async def _maybe_spawn_impl_run(self, design_run: ExecutionRun) -> None:
        """P1-L2: chain an IMPLEMENTATION run after a verified DESIGN run.

        Fires only when ALL hold:
        * the workspace has run-routing rules — i.e. it has OPTED IN to the
          rule-routed / executor execution model. A rule-less workspace (every
          existing single-account workspace) keeps today's single-run behaviour:
          chaining a design→impl pair onto one model would just run the work
          twice. The handoff is meaningful only when stages route distinctly.
        * the run's frame marks the pipeline ``design_then_impl``;
        * this run is NOT itself the impl stage (so the impl run can't spawn
          another — the chain is exactly two runs).

        The new run is OPEN (the next AgentWorker tick frames + drives it),
        carries ``stage="impl"`` so routing targets the impl executor, and is
        seeded with the design run's id + produced artifact refs so its context
        (P1-L2b) can read the design spec.
        """
        payload = design_run.payload if isinstance(design_run.payload, dict) else {}
        raw_frame = payload.get("frame")
        frame = raw_frame if isinstance(raw_frame, dict) else {}
        if frame.get("pipeline") != "design_then_impl":
            return
        if payload.get("stage") == "impl":
            return
        if not await self._workspace_has_routing_rules(design_run.workspace_id):
            return

        refs = await self._design_artifact_refs(design_run.id)
        # Capture the spec TEXT now — the design worktree is guaranteed present at
        # this transition (REVIEW_READY, pre-cleanup) and, if the design auto-
        # shipped just above, its spec is also in product main. Inlining here is
        # durable: reading refs at the impl run's DISPATCH time (later) raced
        # worktree cleanup + a held design run whose spec never reached main →
        # has_spec=false (findings 2026-07-01, D-2). refs kept for provenance /
        # back-compat fallback.
        spec_text = capture_design_spec_text(
            product_id=design_run.product_id,
            design_run_id=design_run.id,
            refs=refs,
            settings=self._settings,
        )
        impl = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=design_run.workspace_id,
            product_id=design_run.product_id,
            request_id=design_run.request_id,
            status=RunStatus.OPEN,
            payload={
                "request_id": (
                    str(design_run.request_id) if design_run.request_id is not None else None
                ),
                "intent_text": payload.get("intent_text"),
                "stage": "impl",
                "pipeline": "design_then_impl",
                "design_run_id": str(design_run.id),
                "design_artifact_refs": refs,
                "design_spec_text": spec_text,
            },
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self._session.add(impl)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=impl.id,
                workspace_id=design_run.workspace_id,
                from_status=None,
                to_status=RunStatus.OPEN,
                reason=f"impl stage spawned from design run {design_run.id}",
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "handoff_impl_run_spawned",
            design_run_id=str(design_run.id),
            impl_run_id=str(impl.id),
            artifact_refs=len(refs),
            has_spec=spec_text is not None,
        )

    async def _workspace_has_routing_rules(self, workspace_id: uuid.UUID) -> bool:
        """True when the workspace has any run-routing rule (it has opted into
        the rule-routed execution model — the gate for design→impl chaining)."""
        return await self._run_routing_rules.has_any(workspace_id=workspace_id)

    async def _design_artifact_refs(self, design_run_id: uuid.UUID) -> list[str]:
        """The artifact_refs the design run's deliverable(s) produced — the
        spec files the impl stage will read. Dedupes, preserves order."""
        rows = await self._deliverables.list_by_run_id(design_run_id)
        refs: list[str] = []
        for row in rows:
            row_payload = row.payload if isinstance(row.payload, dict) else {}
            for ref in row_payload.get("artifact_refs") or []:
                if isinstance(ref, str) and ref not in refs:
                    refs.append(ref)
        return refs


__all__ = ["AgentRunner"]
