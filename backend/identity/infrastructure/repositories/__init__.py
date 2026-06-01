"""Concrete Repository implementations — :mod:`backend.identity` (Lift I-Repo-Identity).

One :class:`SqlAlchemy<Entity>Repository` per domain Protocol. Constructor
takes one :class:`AsyncSession`; the session owns the transaction (v8 D45).
"""

from __future__ import annotations

from backend.identity.infrastructure.repositories.membership_repository_sql import (
    SqlAlchemyMembershipRepository,
)
from backend.identity.infrastructure.repositories.resource_binding_repository_sql import (
    SqlAlchemyResourceBindingRepository,
)
from backend.identity.infrastructure.repositories.user_repository_sql import (
    SqlAlchemyUserRepository,
)
from backend.identity.infrastructure.repositories.workspace_repository_sql import (
    SqlAlchemyWorkspaceRepository,
)

__all__ = [
    "SqlAlchemyMembershipRepository",
    "SqlAlchemyResourceBindingRepository",
    "SqlAlchemyUserRepository",
    "SqlAlchemyWorkspaceRepository",
]
