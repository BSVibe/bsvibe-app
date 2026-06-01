"""MembershipRepository Protocol — read/write seam for :class:`MembershipRow`.

v8 D44/D45. :class:`MembershipRow` is the user ↔ workspace edge with a
role. Auth resolves the caller's active membership on every request;
the workspaces router enforces "is the caller a member of this workspace?";
the GDPR export materialises memberships into the workspace doc.

An *active* membership is one with ``left_at IS NULL``.

Method surface limited to the existing callers. New methods get added
per real caller, never speculatively.

Concrete impl:
:mod:`backend.identity.infrastructure.repositories.membership_repository_sql`.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.identity.db import MembershipRow


@runtime_checkable
class MembershipRepository(Protocol):
    """Persistence seam for :class:`MembershipRow` rows."""

    async def first_active_for_user(self, user_id: uuid.UUID) -> MembershipRow | None:
        """Return the user's oldest active membership, or ``None``.

        ``joined_at`` ascending — auth uses this to resolve "the caller's
        active workspace" deterministically when a user is in several.
        """

    async def active_for_user_in_workspace(
        self, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> MembershipRow | None:
        """Return the user's active membership in ``workspace_id``, or ``None``.

        ``UniqueConstraint(user_id, workspace_id)`` guarantees at most one row
        (active or otherwise) per pair; this filter narrows to the active one.
        """

    async def add(self, membership: MembershipRow) -> None:
        """Stage a new membership for INSERT on the next flush.

        The repository does NOT flush or commit; the caller owns the
        transaction boundary (v8 D45).
        """


__all__ = ["MembershipRepository"]
