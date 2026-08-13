"""CI-green auto-merge runtime wiring (PR4).

Isolated here (rather than in ``delivery_runtime``) so the only module that
reaches the github REST client through this file is ``worker_runtime`` — NOT the
inbound engine layer (``backend.api.webhooks`` / ``backend.connectors.resolver``,
which reach ``delivery_runtime`` via the approval-callback chain). Keeping the
``plugin.github.client`` dependency off that chain preserves the R2c
import-linter contract (the inbound layer has zero plugin imports).

Two pieces:

* :func:`build_merge_watch_client_resolver` — the production per-row GitHub
  client resolver (workspace github binding → decrypted API token → a
  :class:`~plugin.github.client.GithubClient`), injected into the worker so the
  infrastructure-layer worker itself never imports the application binding
  resolver.
* :func:`build_merge_watch_workers` — the gated worker construction: returns the
  :class:`MergeWatchWorker` iff ``github_auto_merge_enabled``, else ``[]``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.delivery.connector_dispatch._resolver import (
    resolve_github_binding,
)
from backend.workflow.application.runtime.merge_watch_freshen import build_branch_freshener
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    ConflictEscalate,
    ConflictRedispatch,
    MergeClientResolver,
    MergeWatchClient,
    MergeWatchWorker,
    MergeWatchWorkerConfig,
    StallEscalate,
)
from plugin.github.client import DEFAULT_BASE_URL, GithubClient

logger = structlog.get_logger(__name__)


async def _product_id_for_run(session: AsyncSession, run_id: uuid.UUID) -> uuid.UUID | None:
    """The product a watched run belongs to, or ``None`` for a product-less run.

    Both resolvers below need it so the binding they hand the worker is the
    run's OWN product repo (#681) — merging or re-cloning against a sibling
    product's repo is the same corruption the provisioner used to cause, just
    later in the pipeline.
    """
    return await session.scalar(select(ExecutionRun.product_id).where(ExecutionRun.id == run_id))


def build_merge_watch_client_resolver(*, cipher: CredentialCipher) -> MergeClientResolver:
    """Build the production per-row GitHub client resolver for the MergeWatchWorker.

    Resolves the github delivery binding of the watched run's PRODUCT + decrypts
    its API token the SAME way :func:`deliver_github` does
    (``resolve_github_binding`` + ``resolve_connector_credentials``), honoring a
    per-connector ``github_api_url`` override (default github.com). Returns
    ``None`` when there is no resolvable github delivery target (the connector was
    removed / deactivated, or none carries the product's repo — #681), which the
    worker maps to a terminal ``failed`` row."""

    async def _resolve(
        session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> MergeWatchClient | None:
        binding = await resolve_github_binding(
            session,
            workspace_id=workspace_id,
            product_id=await _product_id_for_run(session, run_id),
        )
        if binding is None:
            return None
        creds = await resolve_connector_credentials(session, account=binding.account, cipher=cipher)
        # Persist any token refresh resolve performed under the hood.
        await session.commit()
        token = creds["token"]
        base_url = str(
            (binding.account.delivery_config or {}).get("github_api_url") or DEFAULT_BASE_URL
        )
        return GithubClient(token, base_url=base_url)

    return _resolve


def build_merge_watch_conflict_redispatch(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> ConflictRedispatch:
    """Build the PR6 conflict re-dispatch callback for the MergeWatchWorker.

    Writing ``run.payload["merge_conflict"]`` + transitioning the run RUNNING →
    OPEN (so ``AgentWorker.drive_once`` re-picks it and the agent resolves the
    conflict — PR7's side) are APPLICATION concerns, so they live here and are
    injected into the infrastructure worker as an opaque callable. Uses the SAME
    RUNNING → OPEN resume seam ``checkpoint_resolution.resolve_checkpoint`` uses.
    Opens its own short transaction (the worker's session holds the per-repo lock)
    and is idempotent — a no-op run id is skipped; a re-open of an already-OPEN
    run no-ops in :meth:`AgentRunner.transition`.
    """

    async def _redispatch(
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None:
        from backend.workflow.application.agent_runner import AgentRunner  # noqa: PLC0415

        async with session_factory() as session:
            run = await session.get(ExecutionRun, run_id)
            if run is None:
                logger.warning("merge_watch_redispatch_run_missing", run_id=str(run_id))
                return
            # Re-assign payload (not in-place mutate) so SQLAlchemy detects the
            # change on the JSON column — mirrors checkpoint_resolution.
            payload = dict(run.payload or {})
            payload["merge_conflict"] = {
                "conflict_paths": list(conflict_paths),
                "base_branch": base_branch,
                "pr_number": pr_number,
            }
            # Conflict-robustness — EVERY re-dispatch delivers the conflict
            # context to the agent afresh: writing ``merge_conflict`` restores the
            # one-shot directive (the drive loop re-injects + re-consumes it), and
            # clearing a stale ``merge_conflict_resolving`` marker (left by a
            # PRIOR turn that consumed the directive but then failed) keeps the
            # retried re-drive clean — the drive loop re-sets the marker itself.
            payload.pop("merge_conflict_resolving", None)
            run.payload = payload
            runner = AgentRunner(session)
            await runner.transition(
                run_id=run_id,
                to_status=RunStatus.OPEN,
                reason=f"merge conflict on PR #{pr_number}: freshen against {base_branch}",
            )
            await session.commit()
        logger.info(
            "merge_watch_conflict_redispatched",
            run_id=str(run_id),
            pr_number=pr_number,
            conflict_paths=conflict_paths,
        )

    return _redispatch


def build_merge_watch_conflict_escalate(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> ConflictEscalate:
    """Build the conflict-robustness escalation callback for the MergeWatchWorker.

    When the bounded re-dispatch retries are exhausted (the agent never re-pushed
    a resolution — its re-drive stalled/failed, e.g. an autodeploy killed the
    claude subprocess mid-run), the conflict must reach the FOUNDER rather than
    park forever. Raising a ``merge_conflict_review`` Decision (the founder is
    notified + gets the retry/discard one-click actions) and pausing the run on
    it are APPLICATION concerns, so they live here and are injected into the
    infrastructure worker as an opaque callable.

    Pauses the run by transitioning it to RUNNING — the "paused on a Decision"
    convention (``AgentWorker.drive_once`` scans OPEN, so a RUNNING run is NOT
    re-picked, and the founder's ``retry`` resumes it RUNNING → OPEN). Clears the
    stale one-shot conflict markers so a founder-guided retry starts clean. Opens
    its own short transaction (the worker's session holds the per-repo lock) and
    is idempotent — a missing run id is a no-op.
    """

    async def _escalate(
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None:
        from backend.workflow.application.agent_runner import AgentRunner  # noqa: PLC0415
        from backend.workflow.application.run_persistence import create_decision  # noqa: PLC0415

        async with session_factory() as session:
            run = await session.get(ExecutionRun, run_id)
            if run is None:
                logger.warning("merge_watch_escalate_run_missing", run_id=str(run_id))
                return
            # Clear the stale one-shot conflict markers (a failed re-drive may
            # have consumed the directive but left ``merge_conflict_resolving``);
            # a founder-guided retry re-enters cleanly.
            payload = dict(run.payload or {})
            payload.pop("merge_conflict", None)
            payload.pop("merge_conflict_resolving", None)
            run.payload = payload
            # Pause the run ON the Decision (RUNNING → not re-picked by
            # drive_once). A no-op when the run is already RUNNING; a wedged
            # OPEN/other-state run is pulled out of the drive loop here.
            runner = AgentRunner(session)
            await runner.transition(
                run_id=run_id,
                to_status=RunStatus.RUNNING,
                reason=(
                    f"merge conflict on PR #{pr_number} unresolved after "
                    "automatic re-dispatch retries — escalated to founder"
                ),
            )
            await create_decision(
                session,
                run,
                None,  # work_step is unused by the Decision row
                kind="merge_conflict_review",
                payload={
                    "reason": "conflict_unresolved_escalated",
                    "conflict_paths": list(conflict_paths),
                    "base_branch": base_branch,
                    "pr_number": pr_number,
                },
                rationale="merge conflict unresolved after automatic re-dispatch retries",
            )
            await session.commit()
        logger.info(
            "merge_watch_conflict_escalated",
            run_id=str(run_id),
            pr_number=pr_number,
        )

    return _escalate


def build_merge_watch_stall_escalate(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> StallEscalate:
    """Build the give-up escalation callback for the MergeWatchWorker.

    The watch has terminals it cannot come back from that NO human asked for —
    the github target went away, or CI never went green before the deadline. Each
    leaves a pull request open with nobody left to merge it. Raising a
    ``merge_watch_stalled`` Decision (which is also what emits the founder
    notification) is an APPLICATION concern, so it lives here and is injected
    into the infrastructure worker as an opaque callable.

    Deliberately does NOT touch the run's status. By the time a PR is under
    watch, its run has shipped — the deliverable landed and the founder approved
    it. Pausing it (the conflict escalation's move) would re-open finished work,
    and cancelling it would deny a delivery that genuinely happened. What is left
    is a report, so this only reports. Opens its own short transaction (the
    worker's session may hold the per-repo lock) and a missing run is a no-op.
    """

    async def _escalate(
        run_id: uuid.UUID,
        *,
        reason: str,
        repo: str,
        pr_number: int,
    ) -> None:
        from backend.workflow.application.run_persistence import create_decision  # noqa: PLC0415

        async with session_factory() as session:
            run = await session.get(ExecutionRun, run_id)
            if run is None:
                logger.warning("merge_watch_stall_run_missing", run_id=str(run_id))
                return
            await create_decision(
                session,
                run,
                None,  # work_step is unused by the Decision row
                kind="merge_watch_stalled",
                payload={"reason": reason, "repo": repo, "pr_number": pr_number},
                rationale=f"merge watch gave up on {repo}#{pr_number}: {reason}",
            )
            await session.commit()
        logger.info(
            "merge_watch_stall_escalated",
            run_id=str(run_id),
            repo=repo,
            pr_number=pr_number,
            reason=reason,
        )

    return _escalate


def build_merge_watch_workers(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    redis_client: Any = None,
) -> list[MergeWatchWorker]:
    """PR4 — the CI-green auto-merge poller, gated on ``github_auto_merge_enabled``.

    Returns ``[MergeWatchWorker]`` when the founder opted in, else ``[]`` — so
    with the flag off the worker is NOT in the runtime's worker set at all and
    ``github_merge_watch`` is never polled.

    ``redis_client`` is what lets a client_attach product's stale PR be freshened
    at all: that git has to run on the founder's machine, and the exec channel to
    it is a Redis dispatch. Without one, such a row reports "could not freshen"
    rather than quietly freshening it HERE — which would clone their source onto
    this machine to fix a mergeability problem (§3.5)."""
    if not settings.github_auto_merge_enabled:
        return []
    cipher = CredentialCipher(_key_from_settings())
    return [
        MergeWatchWorker(
            session_factory=session_factory,
            client_resolver=build_merge_watch_client_resolver(cipher=cipher),
            branch_freshener=build_branch_freshener(
                cipher=cipher,
                session_factory=session_factory,
                redis_client=redis_client,
                run_workspace_root=Path(settings.run_workspace_root),
            ),
            redispatch_conflict=build_merge_watch_conflict_redispatch(
                session_factory=session_factory
            ),
            escalate_conflict=build_merge_watch_conflict_escalate(session_factory=session_factory),
            escalate_stall=build_merge_watch_stall_escalate(session_factory=session_factory),
            config=MergeWatchWorkerConfig(
                poll_interval_s=settings.github_auto_merge_poll_interval_s,
                conflict_resolution_deadline_s=settings.github_conflict_resolution_deadline_s,
                conflict_max_redispatch=settings.github_conflict_max_redispatch,
            ),
        )
    ]


__all__ = [
    "build_branch_freshener",
    "build_merge_watch_client_resolver",
    "build_merge_watch_conflict_escalate",
    "build_merge_watch_conflict_redispatch",
    "build_merge_watch_stall_escalate",
    "build_merge_watch_workers",
]
