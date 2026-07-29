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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.delivery.connector_dispatch._resolver import (
    resolve_github_binding,
)
from backend.workflow.infrastructure.workers.merge_watch_worker import (
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
    return [
        MergeWatchWorker(
            session_factory=session_factory,
            client_resolver=build_merge_watch_client_resolver(
                cipher=CredentialCipher(_key_from_settings())
            ),
            config=MergeWatchWorkerConfig(
                poll_interval_s=settings.github_auto_merge_poll_interval_s
            ),
        )
    ]


__all__ = [
    "build_merge_watch_client_resolver",
    "build_merge_watch_workers",
]
