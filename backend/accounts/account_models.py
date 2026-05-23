"""SQLAlchemy schema for the billing ``Account`` (the orthogonal account axis).

Distinct from :class:`backend.accounts.models.ModelAccount` (an LLM provider
credential): an ``Account`` is the *partition key* on which model accounts,
rules, and intents are scoped via ``X-BSVibe-Account-Id``. v1 provisions
exactly one **personal** account per workspace at login bootstrap — invisible
infra, never a founder-facing surface. There is intentionally NO unique
constraint on ``workspace_id`` so a workspace can grow multiple accounts
later; resolution picks the earliest-created one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

AccountsBase = Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Account(Base):
    """A billing/partition account scoped to one workspace (the personal one
    in v1). Seeded at bootstrap; resolution is earliest-created-wins."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="personal")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
