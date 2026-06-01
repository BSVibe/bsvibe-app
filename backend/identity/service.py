"""Identity + workspace bootstrap (Workflow §10.1).

Minimal bootstrap only: on first successful login the Supabase subject is
upserted into ``users`` and, if the user has no active membership, a personal
``Workspace`` + ``Membership(role='owner')`` is created. The onboarding steps
(§10.3 model→product→connector→direction) and the vault / BSage partition are
out of scope for this chunk.

Lift I-Repo-Identity (v8 §22 #11 + D44/D45): persistence access here goes
through :class:`UserRepository`, :class:`MembershipRepository`, and
:class:`WorkspaceRepository` rather than raw ``select`` / ``session.get``.
Transaction boundaries are unchanged — :func:`ensure_user_bootstrapped` still
owns the commit; the Repositories never commit.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.db import MembershipRow, UserRow
from backend.identity.infrastructure.repositories import (
    SqlAlchemyMembershipRepository,
    SqlAlchemyUserRepository,
    SqlAlchemyWorkspaceRepository,
)
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.account_service import ensure_personal_account


def _default_workspace_name(email: str | None) -> str:
    if email and "@" in email:
        return f"{email.split('@', 1)[0]}'s workspace"
    return "My workspace"


async def get_user_by_supabase_id(session: AsyncSession, supabase_user_id: str) -> UserRow | None:
    """Return the :class:`UserRow` whose Supabase subject is ``supabase_user_id``.

    Thin pass-through onto :class:`SqlAlchemyUserRepository.get_by_supabase_id`,
    kept as a top-level coroutine for back-compat with the many callers
    (deps.py, the API routes) that import this name directly.
    """
    return await SqlAlchemyUserRepository(session).get_by_supabase_id(supabase_user_id)


async def active_membership_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> MembershipRow | None:
    """Return the user's oldest active membership, or ``None``.

    Thin pass-through onto
    :class:`SqlAlchemyMembershipRepository.first_active_for_user`.
    """
    return await SqlAlchemyMembershipRepository(session).first_active_for_user(user_id)


async def resolve_workspace_id(session: AsyncSession, *, supabase_user_id: str) -> uuid.UUID | None:
    """Return the active workspace for a Supabase subject, or ``None``."""
    user = await get_user_by_supabase_id(session, supabase_user_id)
    if user is None:
        return None
    membership = await active_membership_for_user(session, user.id)
    return membership.workspace_id if membership is not None else None


async def _get_or_create_user(
    session: AsyncSession, supabase_user_id: str, email: str | None
) -> UserRow:
    """Return the user row, creating it if absent.

    On a concurrent first-login the insert may collide with the unique
    ``supabase_user_id``; that ``IntegrityError`` is caught and the row the
    winner created is re-fetched and reused. The rollback is safe because
    bootstrap is the first DB work in the request transaction.
    """
    users = SqlAlchemyUserRepository(session)
    user = await users.get_by_supabase_id(supabase_user_id)
    if user is not None:
        if email and user.email != email:
            user.email = email
        return user

    user = UserRow(id=uuid.uuid4(), supabase_user_id=supabase_user_id, email=email)
    await users.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await users.get_by_supabase_id(supabase_user_id)
        if existing is None:  # pragma: no cover — a unique violation implies it exists
            raise
        return existing
    return user


async def ensure_user_bootstrapped(
    session: AsyncSession,
    *,
    supabase_user_id: str,
    email: str | None,
    region: str = "us-1",
) -> tuple[UserRow, MembershipRow]:
    """Upsert the user and guarantee they own at least one workspace (§10.1).

    Idempotent: a returning user with an existing membership keeps it; only a
    brand-new or workspace-less user gets a fresh Workspace + owner Membership.
    Commits before returning.

    Concurrency-safe for the first-login race: a duplicate user insert is
    caught + re-resolved, and the membership bootstrap is serialized with a
    ``SELECT … FOR UPDATE`` on the user row (a no-op on SQLite, where the test
    suite runs single-connection). Two simultaneous first-logins therefore
    converge on one user + one workspace.
    """
    users = SqlAlchemyUserRepository(session)
    memberships = SqlAlchemyMembershipRepository(session)
    workspaces = SqlAlchemyWorkspaceRepository(session)

    user = await _get_or_create_user(session, supabase_user_id, email)

    # Serialize the membership bootstrap on the user row: the second of two
    # racing logins blocks here until the first commits, then sees its
    # membership and skips creating a duplicate workspace.
    await users.lock_for_update(user.id)

    membership = await memberships.first_active_for_user(user.id)
    if membership is None:
        workspace = WorkspaceRow(
            id=uuid.uuid4(),
            name=_default_workspace_name(email),
            region=region,
            safe_mode=True,
        )
        await workspaces.add(workspace)
        await session.flush()
        membership = MembershipRow(
            id=uuid.uuid4(),
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        await memberships.add(membership)
        await session.flush()

    # Seed (or backfill) the workspace's personal billing account so the
    # model-accounts surface (X-BSVibe-Account-Id) has a real id to partition
    # on. Runs on EVERY login — idempotent — so pre-feature users who already
    # own a workspace but no Account get one here. Folded into the bootstrap
    # commit below.
    await ensure_personal_account(session, workspace_id=membership.workspace_id)

    await session.commit()
    return user, membership
