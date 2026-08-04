"""A refresh performed during the bootstrap clone MUST be persisted.

Regression cover for the #679 follow-up: ``_resolve_clone_token`` resolved the
github credential in its own session but never committed it. When
``resolve_connector_credentials`` refreshed an expiring token under the hood,
GitHub ROTATED the refresh token server-side while our new pair was only staged
in the uncommitted session — closing it rolled the write back.

The next caller then presented the already-invalidated refresh token and got
``bad_refresh_token``, flipping the connector to ``needs_reauth``. The clone
itself succeeded, so the damage was silent: bootstrap worked and bricked the
workspace's github connector on the way out.

The delivery path has always committed here (``_github.py``: "Persist any token
refresh resolve performed under the hood."). Bootstrap must do the same.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.connectors.auth.providers as providers_mod
from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.auth.providers import register_provider
from backend.connectors.auth.tokenset import TokenSet
from backend.connectors.db import ConnectorAccountRow, ConnectorsBase
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    run_product_bootstrap_job,
)
from backend.workflow.infrastructure.delivery.git_ops import GitError

from .._support import db_engine

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"
OLD_ACCESS = "gho_old_access"
OLD_REFRESH = "ghr_old_refresh"
NEW_ACCESS = "gho_rotated_access"
NEW_REFRESH = "ghr_rotated_refresh"


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase, ConnectorsBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _kms_key():
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    previous = settings.gateway_kms_key_b64
    object.__setattr__(
        settings, "gateway_kms_key_b64", base64.urlsafe_b64encode(KEY).decode("ascii")
    )
    try:
        yield
    finally:
        object.__setattr__(settings, "gateway_kms_key_b64", previous)


@pytest.fixture(autouse=True)
def _rotating_github_provider():
    """A github provider whose refresh ROTATES both tokens, like the real one."""
    snapshot = dict(providers_mod._REGISTRY)

    provider = MagicMock()
    provider.name = "github"
    provider.refreshable = True
    provider.refresh = AsyncMock(
        return_value=TokenSet(
            access_token=NEW_ACCESS,
            refresh_token=NEW_REFRESH,
            expires_at=datetime.now(tz=UTC) + timedelta(hours=8),
            scopes=[],
            account_label=None,
        )
    )
    register_provider(provider)
    try:
        yield provider
    finally:
        providers_mod._REGISTRY.clear()
        providers_mod._REGISTRY.update(snapshot)


async def test_bootstrap_persists_a_rotated_github_token(session_factory, tmp_path):
    """After bootstrap, the DB holds the ROTATED pair — not the pre-refresh one."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    cipher = CredentialCipher(KEY)
    account_id = uuid.uuid4()

    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=True))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://github.com/owner/private-repo",
            )
        )
        s.add(
            ConnectorAccountRow(
                id=account_id,
                workspace_id=workspace_id,
                connector="github",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("legacy-secret"),
                delivery_config={},
                is_active=True,
            )
        )
        s.add(
            ConnectorOAuthTokenRow(
                connector_account_id=account_id,
                provider="github",
                access_token_ciphertext=cipher.encrypt(OLD_ACCESS),
                refresh_token_ciphertext=cipher.encrypt(OLD_REFRESH),
                # Already past → resolve refreshes under the hood.
                expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
                status="active",
            )
        )
        await s.commit()

    from backend.config import get_settings  # noqa: PLC0415

    object.__setattr__(get_settings(), "product_workspace_root", str(tmp_path))

    fake_git = MagicMock()
    # Stop right after the clone — this test is about the credential side effect.
    fake_git.clone = AsyncMock(side_effect=GitError("stop after clone"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/private-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    # The clone used the refreshed access token...
    assert fake_git.clone.await_args.kwargs["token"] == NEW_ACCESS

    # ...and the rotation SURVIVED the job. Without the commit, the row still
    # holds OLD_REFRESH while github has already invalidated it — the exact
    # state that bricked the live connector into needs_reauth.
    async with session_factory() as s:
        row = (
            await s.execute(
                select(ConnectorOAuthTokenRow).where(
                    ConnectorOAuthTokenRow.connector_account_id == account_id
                )
            )
        ).scalar_one()
        assert cipher.decrypt(row.refresh_token_ciphertext) == NEW_REFRESH
        assert cipher.decrypt(row.access_token_ciphertext) == NEW_ACCESS
