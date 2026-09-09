"""SQLAlchemy schema for the supervisor audit subsystem.

One table:

- ``audit_outbox`` — the in-transaction outbox row inserted by the
  emitter (lifted from ``bsvibe_audit.outbox.schema``); a later relay
  ships rows to the central auth-server audit endpoint.

There used to be a second, ``audit_events`` — lifted from BSupervisor as a
denormalised query-friendly mirror, never pointed at by anything, 0 rows in
prod for its whole life. Dropped in
``20260909_drop_producerless_audit_events``; that migration carries the
measurement and the reason.

``SupervisorBase`` and ``AuditOutboxBase`` remain distinct aliases so a single
Alembic ``target_metadata`` keeps covering this module the way it always did.
"""

from __future__ import annotations

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
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

SupervisorBase = Base


AuditOutboxBase = Base


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
