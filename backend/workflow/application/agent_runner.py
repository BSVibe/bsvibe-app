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
``DeliverableRepository`` (Lift I-Repo seam). The post-transition
reactions (``_auto_ship_product_run``, ``_maybe_spawn_next_step``) are
tightly coupled to the transition that
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
from backend.workflow.application.agent_loop import LoopResult, RunCompute
from backend.workflow.application.handoff import capture_prior_step_output
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


async def delivers_via_local_product_repo(session: AsyncSession, run: ExecutionRun) -> bool:
    """True iff this run's work ships by fast-forwarding the LOCAL product repo.

    Ownership is decided by the SAME source the workspace provisioner branches
    on — the workspace's github delivery binding. A github-bound run was
    provisioned as a clone of the github repo and delivers via the push+PR path;
    the local ``merge_to_main`` fast-forward can only fail there (issue #362), so
    the gate must skip it.

    This deliberately does NOT infer ownership from the workspace's filesystem
    shape (``.git`` being a gitdir-pointer FILE vs a DIRECTORY), which is what it
    used to do. That shape is an accident of today's provisioning: local products
    get a linked worktree only because their repo happens to live on the same
    disk. The moment they are materialised as full clones from a remote bundle,
    a shape check would silently stop auto-shipping EVERY local product. The
    filesystem is still consulted for one honest question — was a workspace
    provisioned at all — so glue tests that bypass the provisioner keep their
    pre-W2 behaviour of staying at REVIEW_READY.
    """
    if run.product_id is None:
        return False
    from backend.storage.product_workspace import run_worktree_path  # noqa: PLC0415

    if not (run_worktree_path(run.id) / ".git").exists():
        return False
    from backend.workflow.application.delivery.connector_dispatch import (  # noqa: PLC0415
        resolve_github_binding,
    )

    # Product-scoped (#681) — the ownership question is "was THIS run's product
    # provisioned as a github clone", and the provisioner resolves the binding
    # with the same ``product_id``. Asking workspace-wide would call a run
    # github-bound because a SIBLING product has a github connector, and skip
    # the local merge_to_main that run actually needs.
    binding = await resolve_github_binding(
        session, workspace_id=run.workspace_id, product_id=run.product_id
    )
    return binding is None


class AgentRunner:
    """Spawn + supervise one ExecutionRun for a Request."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        run_repository: RunRepository | None = None,
        deliverable_repository: DeliverableRepository | None = None,
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
        한때 있던 knowledge-only 오케스트레이터.
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
        #
        # #886-redo: record the SAME verdict onto ``run.payload`` under the
        # predicate's own name. A github-bound run answers False here (no
        # local auto-ship path — it delivers via push+PR, issue #362) and
        # stays at REVIEW_READY; ``run_delivery_resolution.auto_resolve_run_on_delivery``
        # reads this recorded answer (never recomputes it) to know it must
        # complete THAT run's SHIPPED transition once its Deliverable
        # actually lands. ``run_delivery_resolution`` must not import
        # ``delivers_via_local_product_repo`` (or anything that reaches it)
        # itself — that predicate transitively imports
        # ``delivery.connector_dispatch``, which reaches
        # ``backend.extensions`` / ``backend.router.accounts.crypto``, and
        # ``run_delivery_resolution`` is reached from the inbound webhook /
        # MCP layer that import-linter's "MCP context depends only on
        # Identity + Workflow + Knowledge + common" contract keeps free of
        # those (confirmed red with `uv run lint-imports` before this fix —
        # see that module's docstring). The predicate itself stays computed
        # in exactly ONE place: here.
        if to_status is RunStatus.REVIEW_READY:
            local_auto_ship = await delivers_via_local_product_repo(self._session, run)
            existing_payload = run.payload if isinstance(run.payload, dict) else {}
            run.payload = {
                **existing_payload,
                "delivers_via_local_product_repo": local_auto_ship,
            }
            if local_auto_ship:
                await self._auto_ship_product_run(run)

        # Step handoff — when a step of a split request reaches its verified
        # terminal, spawn the next one (seeded with this run's id + produced
        # refs). Walks the frame's step plan; a no-op for an unsplit run or the
        # last step.
        if to_status is RunStatus.REVIEW_READY:
            await self._maybe_spawn_next_step(run)
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
                # Publish the merged tree to the product's durable off-box home,
                # INSIDE the lock: two concurrent ships must not publish out of
                # order and leave the older tree as the product's record.
                await self._push_bundle_best_effort(product_id, run)
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

    async def _push_bundle_best_effort(self, product_id: uuid.UUID, run: ExecutionRun) -> None:
        """Publish the product to its durable home without ever failing the ship.

        The publish MERGES onto whatever the store currently holds rather than
        overwriting it, so work published by anyone else between this box
        materialising its copy and now survives.

        Two non-clean paths, both of which leave the local repo authoritative:

        * **transient failure** (object-store outage) — logged loud, ship
          proceeds. The merge into ``main`` already succeeded and the work is
          safe on disk, so stranding the run at REVIEW_READY would re-run the
          whole verify→ship cycle for a problem unrelated to the run. A bundle
          is a whole-repo snapshot, not a delta, so the next ship self-heals.
        * **merge conflict** — nothing is published (a half-merged bundle is
          worse than a stale one) and a ``merge_conflict_review`` Decision is
          raised so the divergence is visible instead of silently growing.

        The step that DELETES the local repo must gate on a successful publish —
        that is what makes this best-effort safe rather than lossy.
        """
        from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
            publish_product_bundle,
        )

        try:
            outcome = await publish_product_bundle(product_id)
        except Exception:  # noqa: BLE001 — durability must not fail the ship
            logger.warning(
                "auto_ship_bundle_push_failed",
                run_id=str(run.id),
                product_id=str(product_id),
                exc_info=True,
            )
            return
        if outcome.status == "conflict":
            await self._raise_bundle_conflict_decision(run, outcome.conflict_paths)

    async def _raise_bundle_conflict_decision(
        self, run: ExecutionRun, conflict_paths: list[str]
    ) -> None:
        """Surface an unpublishable divergence as a founder Decision.

        Mirrors the github side's ``merge_conflict_review`` escalation: the
        conflict is between this product's local history and what its durable
        home already holds, and only a human can say which side wins. Raising it
        keeps the product visible instead of quietly diverging until someone
        notices the bundle is months stale.
        """
        from backend.workflow.application.run_persistence import (  # noqa: PLC0415
            create_decision,
        )

        try:
            await create_decision(
                self._session,
                run,
                None,
                kind="merge_conflict_review",
                payload={
                    "reason": "product_bundle_publish_conflict",
                    "conflict_paths": list(conflict_paths),
                    "product_id": str(run.product_id),
                },
                rationale=(
                    "the product's durable copy has diverged from this box's copy "
                    "and the two cannot be merged automatically"
                ),
            )
            await self._session.flush()
        except Exception:  # noqa: BLE001 — the ship itself already succeeded
            logger.warning(
                "bundle_conflict_decision_failed",
                run_id=str(run.id),
                exc_info=True,
            )

    async def _maybe_spawn_next_step(self, run: ExecutionRun) -> None:
        """Chain the NEXT step of a split request, if there is one.

        Fires when the frame split this request into steps
        (:class:`~backend.workflow.application.stages.frame.FrameStep`) and this
        run is not the last of them. The chain's length and its stage names come
        from the founder's routing rules — this helper walks the list, it does
        not judge.

        The new run is OPEN (the next AgentWorker tick drives it — it is NOT
        re-framed, the plan was decided once), carries the step's ``stage`` so
        routing can target it, works under the step's OWN ``intent``, and is
        seeded with the prior run's id + produced artifact refs + their captured
        text so its context can read what came before.
        """
        payload = run.payload if isinstance(run.payload, dict) else {}
        raw_frame = payload.get("frame")
        frame = raw_frame if isinstance(raw_frame, dict) else {}
        steps = frame.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            return
        index = payload.get("step_index")
        index = index if isinstance(index, int) else 0
        if index + 1 >= len(steps):
            return
        nxt = steps[index + 1]
        if not isinstance(nxt, dict):
            return
        stage = nxt.get("stage")
        intent = nxt.get("intent")
        if not isinstance(stage, str) or not isinstance(intent, str) or not intent.strip():
            logger.warning("handoff_next_step_malformed", run_id=str(run.id), step_index=index + 1)
            return

        refs = await self._prior_artifact_refs(run.id)
        # Capture the output TEXT now — this run's worktree is guaranteed present
        # at this transition (REVIEW_READY, pre-cleanup) and, if it auto-shipped
        # just above, its files are also in product main. Inlining here is
        # durable: reading refs at the next run's DISPATCH time raced worktree
        # cleanup + a held run whose output never reached main (findings
        # 2026-07-01, D-2). refs kept for provenance / back-compat fallback.
        output_text = capture_prior_step_output(
            product_id=run.product_id,
            prior_run_id=run.id,
            refs=refs,
            settings=self._settings,
        )
        nxt_run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            product_id=run.product_id,
            request_id=run.request_id,
            status=RunStatus.OPEN,
            payload={
                "request_id": (str(run.request_id) if run.request_id is not None else None),
                # The founder's OWN words, carried whole through every step.
                # Replacing them with the step's brief would drop every
                # requirement the framer's summary happened to leave out —
                # #690 measured that loss when the directive was merely
                # truncated: the agent built the half it received, and lint and
                # tests passed on that half.
                "intent_text": payload.get("intent_text"),
                # What THIS step must accomplish. Scope, not a replacement.
                "step_intent": intent.strip(),
                "stage": stage,
                "step_index": index + 1,
                # The plan travels so the step AFTER this one can follow, and so
                # the worker does not re-frame (which would re-decide the split
                # mid-chain against rules that may have changed).
                "frame": frame,
                "prior_run_id": str(run.id),
                "prior_artifact_refs": refs,
                "prior_output_text": output_text,
            },
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self._session.add(nxt_run)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=nxt_run.id,
                workspace_id=run.workspace_id,
                from_status=None,
                to_status=RunStatus.OPEN,
                reason=f"step {index + 2}/{len(steps)} ({stage}) spawned from run {run.id}",
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "handoff_next_step_spawned",
            prior_run_id=str(run.id),
            next_run_id=str(nxt_run.id),
            stage=stage,
            step_index=index + 1,
            step_count=len(steps),
            artifact_refs=len(refs),
            has_prior_output=output_text is not None,
        )

    async def _prior_artifact_refs(self, run_id: uuid.UUID) -> list[str]:
        """The artifact_refs this run's deliverable(s) produced — what the next
        step will read. Dedupes, preserves order."""
        rows = await self._deliverables.list_by_run_id(run_id)
        refs: list[str] = []
        for row in rows:
            row_payload = row.payload if isinstance(row.payload, dict) else {}
            for ref in row_payload.get("artifact_refs") or []:
                if isinstance(ref, str) and ref not in refs:
                    refs.append(ref)
        return refs


__all__ = ["AgentRunner"]
