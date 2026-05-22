"""Workspace + Product persistence schema (Workflow §3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

WorkspacesBase = Base


class WorkspaceRow(WorkspacesBase):
    """Top-level multi-tenancy unit."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Region per Workflow §2.3 — vault/<region>/<workspace_id>/ FS layout
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="us-1")
    safe_mode: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )
    # Soft delete (Workflow §10.7): set on delete; the 30-day-window hard
    # purge + full cascade is a retention-infra follow-up.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductRow(WorkspacesBase):
    """Per-workspace shipping unit."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("workspace_id", "slug", name="uq_products_ws_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )
