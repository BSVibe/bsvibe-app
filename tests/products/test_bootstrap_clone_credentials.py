"""Bootstrap clone authenticates — private repos need the github credential.

Regression cover for #678: ``run_product_bootstrap_job`` used to call
``git_ops.clone(..., token=None)`` unconditionally, so a private ``repo_url``
always failed with ``failed:clone`` even when the workspace carried an active
github connector. The delivery path (``connector_dispatch/_github.py``) already
resolved the same credential — bootstrap simply never asked for it.

The contract this file pins:

* an active github connector → its token reaches ``clone``
* no connector → ``token=None`` (the anonymous path public repos rely on stays)
* a credential-less clone failure explains *why* instead of leaving the founder
  to guess between "private repo" and "typo in the URL"
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.db import ConnectorAccountRow, ConnectorsBase
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    STATUS_FAILED_CLONE,
    run_product_bootstrap_job,
)
from backend.workflow.infrastructure.delivery.git_ops import GitError

from .._support import db_engine

pytestmark = pytest.mark.asyncio

KEY = b"0123456789abcdef0123456789abcdef"
TOKEN = "ghs_bootstrap_clone_token"


@pytest_asyncio.fixture
async def session_factory():
    # Connector rows live in a second declarative base — bootstrap now reads
    # both, so the test schema must carry both.
    async with db_engine(WorkspacesBase, ConnectorsBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _kms_key():
    """Point the credential cipher at a deterministic test key."""
    import base64  # noqa: PLC0415

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


async def _seed(
    session_factory,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    with_connector: bool,
    connector: str = "github",
    is_active: bool = True,
) -> None:
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
        if with_connector:
            s.add(
                ConnectorAccountRow(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    connector=connector,
                    webhook_token=uuid.uuid4().hex,
                    signing_secret_ciphertext=CredentialCipher(KEY).encrypt(TOKEN),
                    # Bootstrap clones the PRODUCT's repo, not the delivery
                    # target — an empty delivery_config must not disqualify the
                    # credential (this is what `resolve_github_binding` would).
                    delivery_config={},
                    is_active=is_active,
                )
            )
        await s.commit()


def _sandbox_workspace_root(tmp_path) -> None:
    from backend.config import get_settings  # noqa: PLC0415

    object.__setattr__(get_settings(), "product_workspace_root", str(tmp_path))


async def test_clone_uses_github_connector_token(session_factory, tmp_path):
    """An active github connector supplies the clone credential (#678)."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=True,
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    # Fail after the credential is read — this test only pins the clone call.
    fake_git.clone = AsyncMock(side_effect=GitError("stop after clone"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/private-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    fake_git.clone.assert_awaited_once()
    assert fake_git.clone.await_args.kwargs["token"] == TOKEN


async def test_clone_stays_anonymous_without_connector(session_factory, tmp_path):
    """No github connector → ``token=None``; public repos keep working."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=False,
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("stop after clone"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/public-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    fake_git.clone.assert_awaited_once()
    assert fake_git.clone.await_args.kwargs["token"] is None


async def test_inactive_connector_is_not_used(session_factory, tmp_path):
    """A revoked (``is_active=False``) connector must not supply a token."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=True,
        is_active=False,
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("stop after clone"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/private-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    assert fake_git.clone.await_args.kwargs["token"] is None


async def test_non_github_connector_is_not_used(session_factory, tmp_path):
    """A telegram connector is not a git credential."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=True,
        connector="telegram",
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("stop after clone"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/private-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    assert fake_git.clone.await_args.kwargs["token"] is None


async def test_failed_clone_without_credential_names_the_cause(session_factory, tmp_path):
    """Anonymous clone failure tells the founder a credential was missing.

    Without this, ``failed:clone`` reads identically for "private repo, no
    connector" and "URL typo" — the founder cannot tell which to fix.
    """
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=False,
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("Cloning into '/app/var/products/x'..."))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/private-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_FAILED_CLONE
        assert row.bootstrap_error is not None
        # The hint names the missing piece, not just the git noise.
        assert "github" in row.bootstrap_error.lower()


async def test_failed_clone_with_credential_has_no_missing_credential_hint(
    session_factory, tmp_path
):
    """With a credential present the failure is NOT attributed to a missing one."""
    workspace_id, product_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        session_factory,
        workspace_id=workspace_id,
        product_id=product_id,
        with_connector=True,
    )
    _sandbox_workspace_root(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock(side_effect=GitError("repository not found"))

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://github.com/owner/missing-repo",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_FAILED_CLONE
        assert row.bootstrap_error is not None
        assert "connect" not in row.bootstrap_error.lower()
