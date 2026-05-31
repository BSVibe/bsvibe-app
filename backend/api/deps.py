"""FastAPI dependencies for v1 routes.

Authentication resolves the verified Supabase principal via
:func:`backend.shared.authz.deps.get_current_user` (raw ES256 JWT, JWKS).
That principal's Supabase subject is mapped to a first-class ``UserRow`` and,
through ``MembershipRow``, to the workspace the request operates within
(Workflow §3). :func:`get_workspace_id` publishes that workspace into the
:data:`backend.data.scoping.current_workspace_id` contextvar so the global
ORM auto-filter (defense layer 2) scopes every SELECT.

The billing ``account_id`` axis is orthogonal to the workspace and is carried
by the ``X-BSVibe-Account-Id`` request header.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Importing scoping installs the do_orm_execute auto-filter listener.
from backend.data.rls import set_workspace_guc
from backend.data.scoping import set_current_workspace_id
from backend.identity.db import MembershipRow, UserRow
from backend.identity.roles import role_satisfies
from backend.identity.service import (
    active_membership_for_user,
    get_user_by_supabase_id,
    resolve_workspace_id,
)
from backend.shared.authz.deps import get_current_user
from backend.shared.authz.types import User
from backend.storage.artifact_store import ArtifactStore, LocalFilesystemArtifactStore

# Re-export so routes / tests refer to one canonical auth dependency.
CurrentUser = Annotated[User, Depends(get_current_user)]

__all__ = [
    "CurrentUser",
    "get_account_id",
    "get_artifact_store",
    "get_current_membership",
    "get_current_user",
    "get_current_user_row",
    "get_db_session",
    "get_workspace_id",
    "require_account_id",
    "require_role",
]


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------
_async_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazily build a process-wide ``async_sessionmaker`` from settings."""
    global _async_engine, _session_factory  # noqa: PLW0603 — module-level singleton intentional
    if _session_factory is not None:
        return _session_factory

    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    _async_engine = create_async_engine(settings.database_url, future=True)
    _session_factory = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped ``AsyncSession``.

    Tests override this dep to inject the test session factory; production
    requests get the shared process-wide engine.
    """
    sf = _get_session_factory()
    async with sf() as session:
        try:
            yield session
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Artifact storage (per-run, swap-ready for R2/S3)
# ---------------------------------------------------------------------------
def get_artifact_store() -> ArtifactStore:
    """Return the per-request :class:`ArtifactStore`.

    Reads ``settings.run_workspace_root`` each call so tests that monkey-patch
    the env / clear ``get_settings.cache_clear()`` see the override take
    effect (the artifact endpoint tests rely on this — they point the root at
    a tmp dir per-test). Construction is cheap (one ``Path.resolve``); no
    singleton needed at this seam.
    """
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    return LocalFilesystemArtifactStore(Path(settings.run_workspace_root))


# ---------------------------------------------------------------------------
# Identity → workspace resolution
# ---------------------------------------------------------------------------
async def get_current_user_row(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserRow:
    """Resolve the authenticated principal to its first-class ``UserRow``.

    403 when the verified subject has no row — i.e. a principal that never
    completed login bootstrap (§10.1). Used by the workspaces router, which
    scopes by the caller's memberships rather than a single active workspace.
    """
    row = await get_user_by_supabase_id(session, user.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no user record for principal",
        )
    return row


async def get_workspace_id(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> uuid.UUID:
    """Resolve + publish the caller's active workspace (defense layers 1+2).

    Maps the Supabase subject → ``UserRow`` → active ``Membership`` →
    ``workspace_id``, sets the request-context contextvar (so the ORM
    auto-filter engages), and returns the id for routes that need it as a
    value. 403 when the caller has no active membership.
    """
    workspace_id = await resolve_workspace_id(session, supabase_user_id=user.id)
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no workspace membership for principal",
        )
    set_current_workspace_id(workspace_id)
    # Defense layer 3 — publish the workspace into the Postgres session GUC
    # so RLS policies on root tables enforce isolation at the DB itself. No-op
    # on SQLite (no GUCs). Uses the session's underlying connection so the
    # GUC + the route's subsequent SELECTs share one PG session.
    conn = await session.connection()
    await set_workspace_guc(conn, workspace_id)
    return workspace_id


# ---------------------------------------------------------------------------
# RBAC — authorization on Membership.role (the third orthogonal axis, after
# authentication via Supabase JWT and isolation via workspace_id scoping).
# ---------------------------------------------------------------------------
async def get_current_membership(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MembershipRow:
    """Resolve the caller's active ``Membership`` in their resolved workspace.

    Also publishes the workspace into the scoping contextvar so a route that
    depends only on this (e.g. via :func:`require_role`) still gets the ORM
    auto-filter. 403 when the caller has no active membership.
    """
    row = await get_user_by_supabase_id(session, user.id)
    membership = await active_membership_for_user(session, row.id) if row is not None else None
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no workspace membership for principal",
        )
    set_current_workspace_id(membership.workspace_id)
    conn = await session.connection()
    await set_workspace_guc(conn, membership.workspace_id)
    return membership


def require_role(minimum: str) -> Callable[..., Awaitable[MembershipRow]]:
    """Build a dependency asserting the caller's role ranks at/above ``minimum``.

    Reads ``Membership.role`` for the caller's resolved workspace and 403s
    when it is below the threshold (``owner > admin > editor > viewer``).
    Returns the membership so a route can reuse it. Authentication is
    unchanged — an unauthenticated caller is still 401'd upstream by
    :func:`get_current_user`; a member-less caller is 403'd by
    :func:`get_current_membership`.
    """

    async def _dep(
        membership: Annotated[MembershipRow, Depends(get_current_membership)],
    ) -> MembershipRow:
        if not role_satisfies(membership.role, minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {minimum!r} or higher required",
            )
        return membership

    return _dep


# ---------------------------------------------------------------------------
# Billing account axis (orthogonal to workspace)
# ---------------------------------------------------------------------------
async def get_account_id(
    x_bsvibe_account_id: Annotated[str | None, Header()] = None,
) -> uuid.UUID | None:
    """Optional billing account id from the ``X-BSVibe-Account-Id`` header."""
    if not x_bsvibe_account_id:
        return None
    try:
        return uuid.UUID(x_bsvibe_account_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid X-BSVibe-Account-Id",
        ) from exc


async def require_account_id(
    account_id: Annotated[uuid.UUID | None, Depends(get_account_id)],
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> uuid.UUID:
    """Resolve the billing account id for account-scoped routes.

    Header wins: a valid ``X-BSVibe-Account-Id`` is used verbatim (preserving
    the orthogonal account axis). When the header is ABSENT the caller's
    personal account is resolved (create-on-read) for the active workspace, so
    a logged-in founder never 400s even before the PWA has fetched the id. A
    malformed header value still 400s upstream in :func:`get_account_id`.
    """
    if account_id is not None:
        return account_id
    from backend.router.accounts.account_service import ensure_personal_account  # noqa: PLC0415

    account = await ensure_personal_account(session, workspace_id=workspace_id)
    await session.commit()
    return account.id
