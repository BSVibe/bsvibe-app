"""The ``tests/production`` tier — INV-3.

Every other suite in this repo overrides ``get_current_user`` /
``get_db_session`` / ``get_workspace_id`` (66 of 70 API test files do). That
means :func:`backend.api.deps.get_workspace_id` — the one dependency that sets
the Postgres RLS GUC AND engages the ORM auto-filter — has **never run through
the API in a test**, so tenant isolation has never been proven end-to-end.

This tier fixes that. It builds the REAL :func:`backend.api.main.create_app`
with an EMPTY ``dependency_overrides`` (asserted by :func:`real_app`), drives
real HTTP routes with a **real JWT the production auth path validates**, and
runs against the CI Postgres with the RLS migration applied.

How the real JWT is minted (the crux)
-------------------------------------
``backend.shared.authz.auth.verify_user_jwt`` verifies the caller's bearer
token against a configured key source. Its documented dev path is
**HS256 + a shared secret** (``USER_JWT_SECRET``; see
``backend/shared/authz/settings.py``). We set that secret via env — a
*configuration* input, NOT a dependency override — and mint an HS256 token
signed with it. ``get_current_user`` then verifies it through the exact
production code path. No auth dependency is overridden; the tier would be
pointless if it were.

Postgres requirement
--------------------
RLS is a no-op on SQLite, so the isolation tests skip unless a real Postgres
is configured + reachable (``BSVIBE_DATABASE_URL`` — set in CI). The
structural scope-audit test (:mod:`test_scope_audit`) needs no DB and runs
everywhere.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .._support import _clean_all_rows, use_real_pg

# The HS256 dev signing secret the tier configures the REAL auth path with.
# Not a production secret — it exists only so a locally-minted token verifies
# through ``verify_user_jwt`` exactly as a Supabase HS256 dev token would.
_USER_JWT_SECRET = "production-tier-hs256-signing-secret"
_GUC = "app.current_workspace_id"

# The isolation tests need a real Postgres (RLS does nothing on SQLite).
requires_real_pg = pytest.mark.skipif(
    not use_real_pg(),
    reason="production tier RLS proof requires a reachable Postgres (BSVIBE_DATABASE_URL)",
)


# ---------------------------------------------------------------------------
# Real auth path configuration (env / settings — NOT a dependency override)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _production_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the REAL verify path for HS256 dev tokens + credential crypto.

    - ``USER_JWT_*`` env → the authz Settings (a separate cache from
      ``backend.config``) so ``verify_user_jwt`` accepts our minted HS256 token.
    - ``gateway_kms_key_b64`` is patched on the *existing* ``backend.config``
      Settings instance (NOT a cache_clear — that would drop the autouse
      ``_isolate_w1_workspace_roots`` tmp-root patch, which shares the same
      instance) so the ModelAccount (app-filter-only table) proof can encrypt
      its api_key.
    """
    from backend.config import get_settings as get_cfg
    from backend.shared.authz.auth import reset_jwks_cache
    from backend.shared.authz.settings import reset_settings_cache

    monkeypatch.setenv("USER_JWT_SECRET", _USER_JWT_SECRET)
    monkeypatch.setenv("USER_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("USER_JWT_AUDIENCE", "bsvibe")
    reset_settings_cache()
    reset_jwks_cache()

    monkeypatch.setattr(
        get_cfg(),
        "gateway_kms_key_b64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
        raising=False,
    )

    yield

    reset_settings_cache()
    reset_jwks_cache()


def mint_jwt(supabase_user_id: str, *, email: str | None = None) -> str:
    """Mint an HS256 session JWT the production ``get_current_user`` accepts.

    Carries the claims ``verify_user_jwt`` requires (``sub`` / ``iat`` / ``exp``)
    and the default audience ``bsvibe``. Signed with the dev secret configured
    by :func:`_production_auth_env`.
    """
    now = int(time.time())
    payload: dict[str, object] = {
        "sub": supabase_user_id,
        "aud": "bsvibe",
        "iat": now,
        "exp": now + 3600,
    }
    if email is not None:
        payload["email"] = email
    return pyjwt.encode(payload, _USER_JWT_SECRET, algorithm="HS256")


# ---------------------------------------------------------------------------
# The REAL app — no overrides
# ---------------------------------------------------------------------------
@pytest.fixture
def real_app() -> object:
    """Build the production app and PROVE its dependency graph is intact.

    The tier's whole point is that ``get_workspace_id`` (GUC + ORM filter) runs
    for real, so an empty ``dependency_overrides`` is a hard invariant here.
    """
    from backend.api.main import create_app

    app = create_app()
    assert app.dependency_overrides == {}, (
        "production tier requires the REAL dependency graph — no auth / session / "
        f"workspace overrides. Found overrides for: "
        f"{[getattr(k, '__name__', k) for k in app.dependency_overrides]}"
    )
    return app


# ---------------------------------------------------------------------------
# Real Postgres session factory (the production singleton, on THIS loop)
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield the process-wide production session factory, rebuilt on this loop.

    ``get_db_session`` is NOT overridden in this tier, so real requests use the
    module singleton in ``backend.api.deps``. asyncpg connections are bound to
    the loop that created them and every pytest-asyncio test gets its own loop,
    so we reset + rebuild the singleton here (and dispose after) to keep all
    connections on one loop. Starts from a clean DB so a prior suite's rows
    can't bleed into the isolation assertions.
    """
    import backend.api.deps as deps

    deps._async_engine = None
    deps._session_factory = None
    factory = deps._get_session_factory()
    engine = deps._async_engine
    assert engine is not None
    await _clean_all_rows(engine)
    try:
        yield factory
    finally:
        deps._async_engine = None
        deps._session_factory = None
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tenant bootstrap + per-tenant client
# ---------------------------------------------------------------------------
async def _reset_guc(session: AsyncSession) -> None:
    """Clear any stale RLS GUC on this session's pooled connection.

    ``set_workspace_guc`` uses ``is_local=false`` so the GUC sticks on a pooled
    connection; a fixture/admin session that reuses that connection must reset
    it to '' (fail-open) or an INSERT into an RLS table whose id != the stale
    GUC would fail the policy WITH CHECK.
    """
    conn = await session.connection()
    if conn.dialect.name == "postgresql":
        await conn.execute(text(f"SELECT set_config('{_GUC}', '', false)"))


async def bootstrap_tenant(
    factory: async_sessionmaker[AsyncSession],
    *,
    supabase_user_id: str,
    email: str,
) -> uuid.UUID:
    """Drive the REAL bootstrap service (the one the /auth/login route calls).

    Returns the tenant's workspace id. Only the external Supabase GoTrue call
    is skipped (it cannot run in CI); user + workspace + owner membership +
    personal account are created by the production
    :func:`ensure_user_bootstrapped`, not by hand-built ``*Row`` objects.
    """
    from backend.identity.service import ensure_user_bootstrapped

    async with factory() as session:
        await _reset_guc(session)
        _user, membership = await ensure_user_bootstrapped(
            session, supabase_user_id=supabase_user_id, email=email
        )
        return membership.workspace_id


def client_for(app: object, token: str) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` speaking to ``app`` as the bearer of ``token``."""
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
