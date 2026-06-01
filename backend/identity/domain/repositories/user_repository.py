"""UserRepository Protocol — read/write seam for :class:`UserRow`.

v8 D44/D45. The user row is the first-class persistence of a Supabase
principal (§10.1). Auth bootstrap looks one up by ``supabase_user_id``;
the workspaces router needs the canonical row to scope memberships.

Method surface limited to the existing callers (auth bootstrap,
the FastAPI ``get_current_user_row`` dependency). New methods get added
per real caller, never speculatively.

Concrete impl:
:mod:`backend.identity.infrastructure.repositories.user_repository_sql`.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.identity.db import UserRow


@runtime_checkable
class UserRepository(Protocol):
    """Persistence seam for :class:`UserRow` rows."""

    async def get(self, user_id: uuid.UUID) -> UserRow | None:
        """Return the user with this id, or ``None``."""

    async def get_by_supabase_id(self, supabase_user_id: str) -> UserRow | None:
        """Return the user with this Supabase subject, or ``None``.

        ``supabase_user_id`` is UNIQUE so at most one row.
        """

    async def add(self, user: UserRow) -> None:
        """Stage a new user for INSERT on the next flush.

        The repository does NOT flush or commit; the caller owns the
        transaction boundary (v8 D45).
        """

    async def lock_for_update(self, user_id: uuid.UUID) -> UserRow | None:
        """Acquire a row-level lock on the user, returning the locked row.

        Powers :func:`ensure_user_bootstrapped`'s first-login serialization
        (§10.1). On PostgreSQL this issues ``SELECT ... FOR UPDATE``;
        on SQLite (the test tier) it is a regular fetch — the test tier
        is single-connection so the lock is unnecessary there.
        """


__all__ = ["UserRepository"]
