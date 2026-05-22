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
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Importing scoping installs the do_orm_execute auto-filter listener.
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

# Re-export so routes / tests refer to one canonical auth dependency.
CurrentUser = Annotated[User, Depends(get_current_user)]

__all__ = [
    "CurrentUser",
    "get_account_id",
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
) -> uuid.UUID:
    """Same as :func:`get_account_id` but 400s if missing — account-scoped routes."""
    if account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="account_id required (pass via X-BSVibe-Account-Id header)",
        )
    return account_id
