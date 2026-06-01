"""FastAPI dependencies for the Router Repository Protocols.

Lift I-Repo-Router. Mirrors :mod:`backend.api.v1._workflow_deps` for the
Router context's two Repositories. REST handlers depend on the Protocols
via ``Depends(get_*_repository)``; tests override via
``app.dependency_overrides`` without touching the session.

Module name is leading-underscore-prefixed so it's clearly an internal
shared dependency module (not a router).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.router.domain.repositories import (
    ModelAccountRepository,
    RunRoutingRuleRepository,
)
from backend.router.infrastructure.repositories import (
    SqlAlchemyModelAccountRepository,
    SqlAlchemyRunRoutingRuleRepository,
)


def get_model_account_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelAccountRepository:
    """One :class:`ModelAccountRepository` per request scope, backed by the request session."""
    return SqlAlchemyModelAccountRepository(session)


def get_run_routing_rule_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRoutingRuleRepository:
    """One :class:`RunRoutingRuleRepository` per request scope, backed by the request session."""
    return SqlAlchemyRunRoutingRuleRepository(session)


__all__ = [
    "get_model_account_repository",
    "get_run_routing_rule_repository",
]
