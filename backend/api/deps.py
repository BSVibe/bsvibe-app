"""FastAPI dependencies for v1 routes.

Authentication resolves the caller through :func:`get_current_principal`,
which accepts BOTH credential classes BSVibe issues — the PWA's session JWT
and the ES256 access token ``bsvibe login`` stores (see
:mod:`backend.api.bearer_auth` for why the gate has to know both, and why
retargeting ``USER_JWT_JWKS_URL`` is not the fix).

A session-JWT principal is mapped by its Supabase subject to a first-class
``UserRow`` and, through ``MembershipRow``, to the workspace the request
operates within (Workflow §3). An access-token principal instead NAMES its
workspace in the token's ``wsp`` claim; the membership is verified but does not
choose the tenant. :func:`get_workspace_id` publishes that workspace into the
:data:`backend.data.scoping.current_workspace_id` contextvar so the global
ORM auto-filter (defense layer 2) scopes every SELECT.

The billing ``account_id`` axis is orthogonal to the workspace and is carried
by the ``X-BSVibe-Account-Id`` request header.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from backend.api.bearer_auth import (
    ApiPrincipal,
    extract_bearer,
    resolve_api_principal,
)

# Importing scoping installs the do_orm_execute auto-filter listener.
from backend.data.engine import create_app_engine
from backend.data.rls import set_workspace_guc
from backend.data.scoping import set_current_workspace_id
from backend.identity.db import MembershipRow, UserRow
from backend.identity.infrastructure.repositories.membership_repository_sql import (
    SqlAlchemyMembershipRepository,
)
from backend.identity.roles import role_satisfies
from backend.identity.service import (
    active_membership_for_user,
    get_user_by_supabase_id,
    resolve_workspace_id,
)
from backend.shared.authz.types import User
from backend.storage.artifact_store import ArtifactStore

__all__ = [
    "ApiPrincipal",
    "CurrentPrincipal",
    "CurrentUser",
    "get_account_id",
    "get_artifact_store",
    "get_current_membership",
    "get_current_principal",
    "get_current_user",
    "get_current_user_row",
    "request_principal",
    "get_db_session",
    "get_db_session_factory",
    "get_output_language",
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
    # Single factory — carries the idle_in_transaction_session_timeout guard.
    _async_engine = create_app_engine(settings.database_url)
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


def get_db_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide :class:`async_sessionmaker`.

    FastAPI dep — used by handlers that schedule background tasks
    (e.g. the Product bootstrap job, Lift A v2) and therefore need a
    session factory that OUTLIVES the request, not a single session.
    Tests override this dep with the test sessionmaker so the background
    task runs against the same DB as the request body.
    """
    return _get_session_factory()


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
    from backend.workflow.application.deliverable_artifact import (  # noqa: PLC0415
        run_artifact_store,
    )

    return run_artifact_store()


# ---------------------------------------------------------------------------
# Authentication — one gate, both credential classes (#1017)
# ---------------------------------------------------------------------------
async def get_current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session: Annotated[AsyncSession, Depends(get_db_session)] = ...,  # type: ignore[assignment]
) -> ApiPrincipal:
    """Resolve the caller from whichever credential class they presented.

    The HTTP method is passed through because an access token's scope gate is
    method-shaped: ``mcp:read`` reads, ``mcp:write`` changes things. A session
    JWT carries no scopes and is unaffected.
    """
    principal = await resolve_api_principal(
        extract_bearer(authorization), method=request.method, session=session
    )
    # Published on the request, not threaded through every downstream dep, so
    # that ``get_current_user`` stays the ONE override seam the suites use.
    # Overriding it replaces this whole dependency subtree; the workspace
    # resolution below then falls back to membership order exactly as it did
    # before there were two credential classes.
    request.state.api_principal = principal
    return principal


CurrentPrincipal = Annotated[ApiPrincipal, Depends(get_current_principal)]


async def get_current_user(principal: CurrentPrincipal) -> User:
    """The authenticated principal's identity, credential class aside."""
    return principal.user


def request_principal(request: Request) -> ApiPrincipal | None:
    """The principal :func:`get_current_principal` published, if it ran.

    ``None`` when a test has overridden the auth seam — the caller then has an
    identity but no token-named workspace, which is the pre-#1017 shape.
    """
    principal = getattr(request.state, "api_principal", None)
    return principal if isinstance(principal, ApiPrincipal) else None


# Re-export so routes / tests refer to one canonical auth dependency.
CurrentUser = Annotated[User, Depends(get_current_user)]


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
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> uuid.UUID:
    """Resolve + publish the caller's active workspace (defense layers 1+2).

    For a session JWT: Supabase subject → ``UserRow`` → active ``Membership`` →
    ``workspace_id``. For an access token the workspace is the one the token
    NAMES (its ``wsp`` claim, already checked against an active membership by
    :func:`backend.api.bearer_auth.resolve_access_token`) — resolving it from
    membership order instead would silently put the CLI in a different tenant
    than the credential it presented.

    Either way this sets the request-context contextvar (so the ORM auto-filter
    engages) and returns the id for routes that need it as a value. 403 when
    the caller has no active membership.
    """
    principal = request_principal(request)
    workspace_id = principal.workspace_id if principal is not None else None
    if workspace_id is None:
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


async def get_output_language(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> str:
    """The caller's workspace OUTPUT language (``ko`` / ``en``) — the language
    founder-facing generated + templated text renders in (``workspaces.language``,
    set via Settings → Language). Defaults to ``en`` (missing row / read hiccup)."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415

    try:
        lang = (
            await session.execute(
                select(WorkspaceRow.language).where(WorkspaceRow.id == workspace_id)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — language is best-effort; never fail the request
        return "en"
    return (lang or "en").strip() or "en"


# ---------------------------------------------------------------------------
# RBAC — authorization on Membership.role (the third orthogonal axis, after
# authentication via Supabase JWT and isolation via workspace_id scoping).
# ---------------------------------------------------------------------------
async def get_current_membership(
    request: Request,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MembershipRow:
    """Resolve the caller's active ``Membership`` in their resolved workspace.

    Mirrors :func:`get_workspace_id`'s split: an access token's membership is
    read in the workspace the token names, a session JWT's is the caller's
    first active one. Also publishes the workspace into the scoping contextvar
    so a route that depends only on this (e.g. via :func:`require_role`) still
    gets the ORM auto-filter. 403 when the caller has no active membership.
    """
    principal = request_principal(request)
    if (
        principal is not None
        and principal.workspace_id is not None
        and principal.user_row_id is not None
    ):
        membership = await SqlAlchemyMembershipRepository(session).active_for_user_in_workspace(
            principal.user_row_id, principal.workspace_id
        )
    else:
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
