"""Repository Protocols — application-layer seam onto Identity persistence.

v8 §22 #11 + D44/D45. The Identity application layer (and every caller
that today goes straight to ``select(UserRow)`` / ``session.get(WorkspaceRow,
...)`` / ``select(MembershipRow)`` etc.) depends on the Protocols here, not
on SQLAlchemy primitives directly. Concrete implementations live in
:mod:`backend.identity.infrastructure.repositories`.

The first Identity Repository extraction (Lift I-Repo-Identity) ships:

* :class:`WorkspaceRepository` — workspace lookup + creation. Used by the
  founder-facing workspace REST surface, intake routing, the Safe Mode
  workspace flag, the GDPR compliance export, and the delivery /
  settle workers.
* :class:`UserRepository` — user lookup + creation. Used by auth bootstrap
  (``ensure_user_bootstrapped``) and the workspaces router (resolve the
  caller's :class:`UserRow`).
* :class:`MembershipRepository` — user ↔ workspace membership. Used by
  access control (active membership for caller, membership for the GDPR
  export, role-based routing).

Deferred to follow-up sub-lifts (I-Repo-Identity-2):

* :class:`TenantRepository` — once the Tenant aggregate has its own row.
* :class:`ConnectorBindingRepository` — once the
  :mod:`backend.workspaces.resource_bindings` workspace ↔ OAuth account
  binding is migrated into the Identity context. The
  :class:`ResourceBindingRepository` already exists at the current path
  (``backend/workspaces/resource_bindings.py``); this lift does NOT move
  it (scope control — see header).

Pragmatic choice (matches Lift I-Repo-Workflow and Lift I-Repo-Knowledge):
SQL repositories return the existing ORM row types
(:class:`UserRow`, :class:`MembershipRow`, :class:`WorkspaceRow`) rather
than separate plain-Python entities. The architectural seam — application
code depending on a Protocol, not on ``sqlalchemy.select`` — is what reduces
the v8 §22 #11 violation count.
"""

from __future__ import annotations

from backend.identity.domain.repositories.membership_repository import (
    MembershipRepository,
)
from backend.identity.domain.repositories.user_repository import UserRepository
from backend.identity.domain.repositories.workspace_repository import (
    WorkspaceRepository,
)

__all__ = [
    "MembershipRepository",
    "UserRepository",
    "WorkspaceRepository",
]
