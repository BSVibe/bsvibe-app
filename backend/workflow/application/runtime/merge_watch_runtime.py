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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.delivery.connector_dispatch._github import github_remote_url
from backend.workflow.application.delivery.connector_dispatch._resolver import (
    resolve_github_binding,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.git_ops import GitOps
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    ConflictRedispatch,
    FreshnessResolver,
    FreshnessTarget,
    MergeClientResolver,
    MergeWatchClient,
    MergeWatchWorker,
    MergeWatchWorkerConfig,
)
from plugin.github.client import DEFAULT_BASE_URL, GithubClient

logger = structlog.get_logger(__name__)


def build_merge_watch_client_resolver(*, cipher: CredentialCipher) -> MergeClientResolver:
    """Build the production per-row GitHub client resolver for the MergeWatchWorker.

    Resolves a workspace's github delivery binding + decrypts its API token the
    SAME way :func:`deliver_github` does (``resolve_github_binding`` +
    ``resolve_connector_credentials``), honoring a per-connector ``github_api_url``
    override (default github.com). Returns ``None`` when the workspace has no
    resolvable github delivery target (the connector was removed / deactivated),
    which the worker maps to a terminal ``failed`` row."""

    async def _resolve(session: AsyncSession, workspace_id: uuid.UUID) -> MergeWatchClient | None:
        binding = await resolve_github_binding(session, workspace_id=workspace_id)
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


def build_merge_watch_freshness_resolver(*, cipher: CredentialCipher) -> FreshnessResolver:
    """Build the PR6 per-row freshness target resolver for the MergeWatchWorker.

    Resolves a workspace's github delivery binding + decrypts its token the SAME
    way :func:`build_merge_watch_client_resolver` does, but returns the git-side
    facts the LOCAL freshness merge needs — ``repo`` / ``base_branch`` / decrypted
    ``token`` / the ``remote_url`` used to re-clone a reaped run workspace — NOT an
    API client. Factoring the binding+token resolution here keeps the
    infrastructure worker free of the application binding resolver + cipher.
    Returns ``None`` when the workspace has no resolvable github delivery target.
    """

    async def _resolve(session: AsyncSession, workspace_id: uuid.UUID) -> FreshnessTarget | None:
        binding = await resolve_github_binding(session, workspace_id=workspace_id)
        if binding is None:
            return None
        creds = await resolve_connector_credentials(session, account=binding.account, cipher=cipher)
        # Persist any token refresh resolve performed under the hood.
        await session.commit()
        return FreshnessTarget(
            repo=binding.repo,
            base_branch=binding.base_branch,
            token=creds["token"],
            remote_url=github_remote_url(binding.repo),
        )

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


def build_merge_watch_workers(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> list[MergeWatchWorker]:
    """PR4 — the CI-green auto-merge poller, gated on ``github_auto_merge_enabled``.

    Returns ``[MergeWatchWorker]`` when the founder opted in, else ``[]`` — so
    with the flag off the worker is NOT in the runtime's worker set at all and
    ``github_merge_watch`` is never polled."""
    if not settings.github_auto_merge_enabled:
        return []
    cipher = CredentialCipher(_key_from_settings())
    return [
        MergeWatchWorker(
            session_factory=session_factory,
            client_resolver=build_merge_watch_client_resolver(cipher=cipher),
            freshness_resolver=build_merge_watch_freshness_resolver(cipher=cipher),
            redispatch_conflict=build_merge_watch_conflict_redispatch(
                session_factory=session_factory
            ),
            git_ops=GitOps(),
            run_workspace_root=Path(settings.run_workspace_root),
            config=MergeWatchWorkerConfig(
                poll_interval_s=settings.github_auto_merge_poll_interval_s
            ),
        )
    ]


__all__ = [
    "build_merge_watch_client_resolver",
    "build_merge_watch_conflict_redispatch",
    "build_merge_watch_freshness_resolver",
    "build_merge_watch_workers",
]
