"""FastAPI dependencies for the Identity Repository Protocols.

Lift I-Repo-Identity. One dependency function per Identity Repository
Protocol; each constructs the concrete :class:`SqlAlchemy*Repository` from
the request's session. REST handlers depend on the Protocol via
``Depends(get_*_repository)``; tests override via
``app.dependency_overrides`` without touching the session.

Module name is leading-underscore-prefixed so it's clearly an internal
shared dependency module (not a router) — matches the
:mod:`backend.api.v1._workflow_deps` and :mod:`backend.api.v1._knowledge_deps`
convention.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.identity.domain.repositories import (
    MembershipRepository,
    UserRepository,
    WorkspaceRepository,
)
from backend.identity.infrastructure.repositories import (
    SqlAlchemyMembershipRepository,
    SqlAlchemyUserRepository,
    SqlAlchemyWorkspaceRepository,
)


def get_workspace_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkspaceRepository:
    """One :class:`WorkspaceRepository` per request scope, backed by the request session."""
    return SqlAlchemyWorkspaceRepository(session)


def get_user_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserRepository:
    """One :class:`UserRepository` per request scope, backed by the request session."""
    return SqlAlchemyUserRepository(session)


def get_membership_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MembershipRepository:
    """One :class:`MembershipRepository` per request scope, backed by the request session."""
    return SqlAlchemyMembershipRepository(session)


__all__ = [
    "get_membership_repository",
    "get_user_repository",
    "get_workspace_repository",
]
