"""Identity persistence — users + memberships (Workflow §3 / §10.1).

``User (1) ── (n) Membership ── (n) Workspace``. v1 operates 1:1 with
``role='owner'``; the schema is N:M-ready so team workspaces ship without a
migration later (``invited_by_user_id`` / ``left_at`` are already here).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

IdentityBase = Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UserRow(IdentityBase):
    """A human principal, keyed to its Supabase identity (§10.1)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supabase_user_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MembershipRow(IdentityBase):
    """A user's membership in a workspace, with role.

    ``left_at`` is a soft-delete marker — an active membership has
    ``left_at IS NULL``. ``invited_by_user_id`` is unused in v1 (no invite
    flow) but present so the team path needs no schema change.
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", name="uq_memberships_user_ws"),)
    # Opt out of the global workspace auto-filter: this IS the table the
    # filter resolves *from*. Auto-scoping it by ``current_workspace_id``
    # would make "list all my workspaces" return only the active one.
    __exclude_workspace_filter__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
