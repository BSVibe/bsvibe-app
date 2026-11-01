"""The intake worker's GitHub permission lookup (#1279).

Lives beside :mod:`.merge_watch_runtime` for the same reason: it reaches the github REST
client, which the infrastructure-layer worker must not import. The workspace's github
binding is the credential (an App token is installation-wide, so any binding serves); the
repo asked about is the event's own.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.data.rls import workspace_session_scope
from backend.router.accounts.crypto import CredentialCipher, cipher_from_settings
from backend.workflow.application.delivery.connector_dispatch._resolver import (
    resolve_github_binding,
)
from backend.workflow.application.intake.trust import RepoPermission
from plugin.github.client import DEFAULT_BASE_URL, GithubClient


def build_repo_permission_lookup(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher | None = None,
) -> RepoPermission:
    """``(workspace_id, "owner/name", login) -> permission``; ``None`` without a github
    binding. Raises on a GitHub or credential failure — the caller trusts no one then."""

    async def permission_of(workspace_id: uuid.UUID, repo: str, login: str) -> str | None:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return None
        # Built on first use: a worker set without a KMS key still starts.
        key = cipher or cipher_from_settings()
        async with session_factory() as session, workspace_session_scope(session, workspace_id):
            binding = await resolve_github_binding(session, workspace_id=workspace_id)
            if binding is None:
                return None
            creds = await resolve_connector_credentials(
                session, account=binding.account, cipher=key
            )
            # Persist any token refresh resolve performed under the hood.
            await session.commit()
        base_url = str(
            (binding.account.delivery_config or {}).get("github_api_url") or DEFAULT_BASE_URL
        )
        return await GithubClient(creds["token"], base_url=base_url).repo_permission(
            owner, name, login
        )

    return permission_of


__all__ = ["build_repo_permission_lookup"]
