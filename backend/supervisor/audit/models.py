"""SQLAlchemy schema for the supervisor audit subsystem.

Two tables:

- ``audit_events`` — denormalised, query-friendly record of every action
  taken by an AI agent (lifted from BSupervisor's ``AuditEvent``).
- ``audit_outbox`` — the in-transaction outbox row inserted by the
  emitter (lifted from ``bsvibe_audit.outbox.schema``); a later relay
  ships rows to the central auth-server audit endpoint.

Both share ``SupervisorBase`` so a single Alembic ``target_metadata``
covers them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SupervisorBase(DeclarativeBase):
    """Declarative base for supervisor-owned tables."""


class AuditOutboxBase(DeclarativeBase):
    """Standalone declarative base for the outbox table (mirror of
    ``bsvibe_audit.outbox.schema.AuditOutboxBase`` — kept separate so a
    caller can still ``create_all`` on either one alone in tests)."""


class AuditEvent(SupervisorBase):
    """One denormalised audit row per AI-agent action."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, default=None
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, default=None
    )
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    target: Mapped[str] = mapped_column(String(1024), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    explanation_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    feedback_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)

    __table_args__ = (
        Index("ix_audit_events_workspace_created_at", "workspace_id", "created_at"),
        Index("ix_audit_events_event_type_created_at", "event_type", "created_at"),
    )


class AuditOutboxRecord(AuditOutboxBase):
    """One pending audit event waiting for relay to the central audit sink."""

    __tablename__ = "audit_outbox"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_letter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_audit_outbox_undelivered", "delivered_at", "next_attempt_at"),)
