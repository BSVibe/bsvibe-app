"""ORM models — Phase 0 baseline.

Only a minimal ``Workspace`` stub exists; richer entities (Membership,
Product, Resource, ConnectorAccount, ModelAccount, ...) land in Phase 1+.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Workspace(SQLModel, table=True):
    __tablename__ = "workspaces"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
