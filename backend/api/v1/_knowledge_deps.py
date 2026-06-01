"""FastAPI dependencies for the Knowledge Repository Protocols.

Lift I-Repo-Knowledge. One dependency function per Knowledge Repository
Protocol; each constructs the concrete :class:`SqlAlchemy*Repository` from
the request's session. REST handlers depend on the Protocol via
``Depends(get_*_repository)``; tests override via
``app.dependency_overrides`` without touching the session.

Module name is leading-underscore-prefixed so it's clearly an internal
shared dependency module (not a router) — matches the
:mod:`backend.api.v1._workflow_deps` convention.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.knowledge.domain.repositories import (
    CanonicalAnchorRepository,
    ProposalRepository,
)
from backend.knowledge.infrastructure.repositories import (
    SqlAlchemyCanonicalAnchorRepository,
    SqlAlchemyProposalRepository,
)


def get_proposal_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProposalRepository:
    """One :class:`ProposalRepository` per request scope, backed by the request session."""
    return SqlAlchemyProposalRepository(session)


def get_canonical_anchor_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CanonicalAnchorRepository:
    """One :class:`CanonicalAnchorRepository` per request scope, backed by the request session."""
    return SqlAlchemyCanonicalAnchorRepository(session)


__all__ = [
    "get_canonical_anchor_repository",
    "get_proposal_repository",
]
