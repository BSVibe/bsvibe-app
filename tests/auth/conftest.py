"""Shared fixtures for auth-wiring tests.

Every test runs against an in-memory SQLite database with the full
``Base.metadata`` materialised (the model modules are imported below so
their tables register). Supabase is always mocked — no test hits the real
external IdP.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import model modules so their tables register on the shared Base.metadata.
from backend.data import Base
from backend.identity.db import MembershipRow, UserRow
from backend.workflow.infrastructure import db as _execution_db  # noqa: F401
from backend.workspaces.db import ProductRow, WorkspaceRow  # noqa: F401

# ruff: noqa: PLC0415


@dataclass
class FakeSupabaseClient:
    """In-memory stand-in for :class:`backend.auth.client.SupabaseAuthClient`.

    Records calls and returns canned sessions so the auth routes can be
    exercised end-to-end without the external IdP.
    """

    user_id: str = "sb-user-1"
    email: str | None = "founder@example.com"
    logout_calls: list[str] = field(default_factory=list)
    refresh_calls: list[str] = field(default_factory=list)
    reset_calls: list[tuple[str, str | None]] = field(default_factory=list)
    authorize_calls: list[tuple[str, str, str]] = field(default_factory=list)
    # When set, ``send_password_reset`` raises it (simulates GoTrue rejecting
    # the recover request) so the route's leak-safe 204 path can be exercised.
    reset_error: Exception | None = None

    def _session(self) -> object:
        from backend.auth.client import SupabaseSession

        return SupabaseSession(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_in=3600,
            supabase_user_id=self.user_id,
            email=self.email,
        )

    async def password_login(self, email: str, password: str) -> object:
        del password
        self.email = email
        return self._session()

    async def exchange_code_for_session(
        self, code: str, code_verifier: str | None = None
    ) -> object:
        del code, code_verifier
        return self._session()

    async def refresh(self, refresh_token: str) -> object:
        self.refresh_calls.append(refresh_token)
        return self._session()

    async def logout(self, access_token: str) -> None:
        self.logout_calls.append(access_token)

    def build_authorize_url(self, provider: str, redirect_to: str, code_challenge: str) -> str:
        self.authorize_calls.append((provider, redirect_to, code_challenge))
        return f"https://fake-supabase/auth/v1/authorize?provider={provider}"

    async def send_password_reset(self, email: str, redirect_to: str | None = None) -> None:
        self.reset_calls.append((email, redirect_to))
        if self.reset_error is not None:
            raise self.reset_error


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def fake_supabase() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_supabase: FakeSupabaseClient,
) -> AsyncIterator[httpx.AsyncClient]:
    """App wired with the test session + mocked Supabase, no auth override.

    Use this for the auth routes (login/callback/refresh/logout) and for
    the unauthenticated-401 assertions.
    """
    from backend.api.deps import get_db_session
    from backend.api.main import create_app
    from backend.auth.client import get_supabase_client

    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_supabase_client] = lambda: fake_supabase

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def authed_client_factory(
    session_factory: async_sessionmaker[AsyncSession],
    fake_supabase: FakeSupabaseClient,
) -> Callable[[str], httpx.AsyncClient]:
    """Build an httpx client whose requests authenticate as ``supabase_user_id``.

    The authz ``get_current_user`` is overridden to return a ``User`` carrying
    that Supabase subject; the membership resolution + workspace scoping then
    run for real against the test DB.
    """
    from backend.api.deps import get_current_user, get_db_session
    from backend.api.main import create_app
    from backend.auth.client import get_supabase_client
    from backend.shared.authz.types import User

    def _build(supabase_user_id: str) -> httpx.AsyncClient:
        app = create_app()

        async def _session() -> AsyncIterator[AsyncSession]:
            async with session_factory() as s:
                yield s

        def _user() -> User:
            return User(id=supabase_user_id, email="x@example.com")

        app.dependency_overrides[get_db_session] = _session
        app.dependency_overrides[get_current_user] = _user
        app.dependency_overrides[get_supabase_client] = lambda: fake_supabase

        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


async def seed_user_workspace(
    session: AsyncSession, *, supabase_user_id: str, role: str = "owner"
) -> tuple[UserRow, WorkspaceRow, MembershipRow]:
    """Insert a User + Workspace + Membership directly (bypassing the route)."""
    user = UserRow(id=uuid.uuid4(), supabase_user_id=supabase_user_id, email="x@example.com")
    ws = WorkspaceRow(id=uuid.uuid4(), name="ws", region="us-1", safe_mode=True)
    session.add(user)
    session.add(ws)
    await session.flush()
    membership = MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, role=role)
    session.add(membership)
    await session.commit()
    return user, ws, membership
