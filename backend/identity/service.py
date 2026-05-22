"""Identity + workspace bootstrap (Workflow §10.1).

Minimal bootstrap only: on first successful login the Supabase subject is
upserted into ``users`` and, if the user has no active membership, a personal
``Workspace`` + ``Membership(role='owner')`` is created. The onboarding steps
(§10.3 model→product→connector→direction) and the vault / BSage partition are
out of scope for this chunk.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.db import MembershipRow, UserRow
from backend.workspaces.db import WorkspaceRow


def _default_workspace_name(email: str | None) -> str:
    if email and "@" in email:
        return f"{email.split('@', 1)[0]}'s workspace"
    return "My workspace"


async def get_user_by_supabase_id(session: AsyncSession, supabase_user_id: str) -> UserRow | None:
    result = await session.execute(
        select(UserRow).where(UserRow.supabase_user_id == supabase_user_id)
    )
    return result.scalar_one_or_none()


async def active_membership_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> MembershipRow | None:
    result = await session.execute(
        select(MembershipRow)
        .where(MembershipRow.user_id == user_id, MembershipRow.left_at.is_(None))
        .order_by(MembershipRow.joined_at.asc())
    )
    return result.scalars().first()


async def resolve_workspace_id(session: AsyncSession, *, supabase_user_id: str) -> uuid.UUID | None:
    """Return the active workspace for a Supabase subject, or ``None``."""
    user = await get_user_by_supabase_id(session, supabase_user_id)
    if user is None:
        return None
    membership = await active_membership_for_user(session, user.id)
    return membership.workspace_id if membership is not None else None


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
    """
    user = await get_user_by_supabase_id(session, supabase_user_id)
    if user is None:
        user = UserRow(id=uuid.uuid4(), supabase_user_id=supabase_user_id, email=email)
        session.add(user)
        await session.flush()
    elif email and user.email != email:
        user.email = email

    membership = await active_membership_for_user(session, user.id)
    if membership is None:
        workspace = WorkspaceRow(
            id=uuid.uuid4(),
            name=_default_workspace_name(email),
            region=region,
            safe_mode=True,
        )
        session.add(workspace)
        await session.flush()
        membership = MembershipRow(
            id=uuid.uuid4(),
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        session.add(membership)
        await session.flush()

    await session.commit()
    return user, membership
