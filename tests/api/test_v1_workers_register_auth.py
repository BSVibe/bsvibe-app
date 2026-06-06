"""Unit tests for :mod:`backend.api.v1.workers_register_auth` — Lift E4.

The resolver decides whether a bearer is a Supabase session JWT or an MCP
ES256 access token and surfaces the resolved workspace. We exercise each
shape's failure branches + the happy paths in isolation so the route handler
can rely on the resolver as a black box.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Register tables.
import backend.executors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.api.v1 import workers_register_auth as auth_mod
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine

# Async resolver tests get the asyncio mark via explicit decoration so the
# small bearer-extractor sync tests don't trigger pytest-asyncio's "async-only"
# warning.


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def test_extract_bearer_returns_none_for_missing() -> None:
    assert auth_mod.extract_bearer(None) is None
    assert auth_mod.extract_bearer("") is None


def test_extract_bearer_returns_none_for_wrong_scheme() -> None:
    assert auth_mod.extract_bearer("Basic abc") is None
    assert auth_mod.extract_bearer("Bearer") is None


def test_extract_bearer_returns_token_for_bearer() -> None:
    assert auth_mod.extract_bearer("Bearer abc.def.ghi") == "abc.def.ghi"


async def test_resolve_raises_for_garbage_bearer(db) -> None:
    async with db() as s:
        with pytest.raises(auth_mod.BearerAuthError):
            await auth_mod.resolve_workspace_for_bearer("clearly-not-a-jwt", s)


async def test_resolve_succeeds_for_supabase_jwt(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake verify_user_jwt → resolve via membership."""
    workspace_id = uuid.uuid4()
    sub = "supa-sub"

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        user_id = uuid.uuid4()
        s.add(UserRow(id=user_id, supabase_user_id=sub, email="x@x"))
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    # Make the MCP path miss + the Supabase path succeed.
    monkeypatch.setattr(
        auth_mod,
        "verify_access_token",
        lambda *a, **k: (_ for _ in ()).throw(__import__("jwt").exceptions.InvalidTokenError("no")),
    )

    fake_claims = {"sub": sub, "exp": 9999999999, "iat": 0}
    monkeypatch.setattr(auth_mod, "verify_user_jwt", lambda *a, **k: fake_claims)

    async with db() as s:
        resolved = await auth_mod.resolve_workspace_for_bearer("fake-jwt", s)
    assert resolved.workspace_id == workspace_id
    assert resolved.auth_kind == "supabase_jwt"


async def test_resolve_fails_when_no_workspace_membership(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = "no-membership-sub"

    async with db() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id=sub, email="x@x"))
        await s.commit()

    monkeypatch.setattr(
        auth_mod,
        "verify_access_token",
        lambda *a, **k: (_ for _ in ()).throw(__import__("jwt").exceptions.InvalidTokenError("no")),
    )
    monkeypatch.setattr(
        auth_mod,
        "verify_user_jwt",
        lambda *a, **k: {"sub": sub, "exp": 9999999999, "iat": 0},
    )

    async with db() as s:
        with pytest.raises(auth_mod.BearerAuthError, match="no workspace membership"):
            await auth_mod.resolve_workspace_for_bearer("fake-jwt", s)
