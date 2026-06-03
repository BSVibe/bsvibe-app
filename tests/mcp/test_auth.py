"""Unit tests for the Bearer-token resolver — Lift D2.

Verifies that ``resolve_principal_from_bearer``:

* accepts a valid token + recognised jti row → returns McpPrincipal
* rejects a tampered / malformed / unknown-kid token → McpAuthError
* rejects a known but revoked token → McpAuthError
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.oauth_db import OAuthAccessTokenRow
from backend.identity.oauth_jwt import issue_access_token
from backend.identity.oauth_keys import get_signing_key, reset_signing_key_for_tests
from backend.mcp.auth import McpAuthError, resolve_principal_from_bearer

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", "http://test")
    get_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            yield s
    get_settings.cache_clear()
    reset_signing_key_for_tests()


def _issue(*, jti: uuid.UUID, user_id: uuid.UUID, workspace_id: uuid.UUID, scope: list[str]) -> str:
    now = int(time.time())
    return issue_access_token(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scope=scope,
        jti=jti,
        issued_at=now,
        expires_at=now + 3600,
        issuer="http://test",
        signing_key=get_signing_key(),
    )


async def _seed_row(session, *, jti, user_id, workspace_id, scope, revoked: bool = False):
    now = datetime.now(UTC)
    row = OAuthAccessTokenRow(
        id=jti,
        workspace_id=workspace_id,
        user_id=user_id,
        client_id="dcr-test",
        scope=scope,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=(now if revoked else None),
    )
    session.add(row)
    await session.commit()
    return row


async def test_resolve_valid_bearer_returns_principal(session) -> None:
    jti = uuid.uuid4()
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    scope = ["mcp:read", "mcp:write"]
    await _seed_row(session, jti=jti, user_id=user_id, workspace_id=workspace_id, scope=scope)
    token = _issue(jti=jti, user_id=user_id, workspace_id=workspace_id, scope=scope)

    principal = await resolve_principal_from_bearer(
        token=token, issuer="http://test", session=session
    )
    assert principal.user_id == user_id
    assert principal.workspace_id == workspace_id
    assert principal.client_id == "dcr-test"
    assert principal.jti == jti
    assert principal.has_scope("mcp:read")
    assert principal.has_scope("mcp:write")
    assert not principal.has_scope("mcp:admin")


async def test_resolve_revoked_token_rejected(session) -> None:
    jti = uuid.uuid4()
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    await _seed_row(
        session,
        jti=jti,
        user_id=user_id,
        workspace_id=workspace_id,
        scope=["mcp:read"],
        revoked=True,
    )
    token = _issue(jti=jti, user_id=user_id, workspace_id=workspace_id, scope=["mcp:read"])

    with pytest.raises(McpAuthError):
        await resolve_principal_from_bearer(token=token, issuer="http://test", session=session)


async def test_resolve_unknown_jti_rejected(session) -> None:
    jti = uuid.uuid4()
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    token = _issue(jti=jti, user_id=user_id, workspace_id=workspace_id, scope=["mcp:read"])
    # No row seeded -> introspection lookup returns None.
    with pytest.raises(McpAuthError):
        await resolve_principal_from_bearer(token=token, issuer="http://test", session=session)


async def test_resolve_malformed_token_rejected(session) -> None:
    with pytest.raises(McpAuthError):
        await resolve_principal_from_bearer(
            token="not-a-real-jwt", issuer="http://test", session=session
        )
