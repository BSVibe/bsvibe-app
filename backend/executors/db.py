"""SQLAlchemy schema for the external executor-worker registration subsystem.

Lift 1 of the executor-pool epic — the registration model ported from
BSGateway (``bsgateway/api/routers/workers.py`` + ``executor/install_token``)
and adapted to monorepo conventions (SQLAlchemy + alembic, ``workspace_id`` as
the tenancy axis, JSON columns portable to the SQLite test tier).

An *external* worker is a remote machine that runs CLI executors
(``claude_code`` / ``codex`` / ``opencode``). It authenticates to the backend
with an opaque per-worker token (only the SHA-256 hash is stored). A worker is
bootstrapped using a per-workspace **install token** — admins mint one, share
it with worker machines, and the machine registers with it.

Distinct from :mod:`backend.workers.db` (the Bundle G internal-daemon liveness
model, table ``workers``). That subsystem tracks the orchestrator's own
consumer-group daemons; this one tracks externally-installed executor hosts.
The names ``workers`` / ``worker_install_tokens`` are already taken there, so
this subsystem owns its own tables: ``executor_workers`` /
``executor_install_tokens``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

ExecutorsBase = Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkerRow(Base):
    """One row per registered external executor worker."""

    __tablename__ = "executor_workers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # JSON (not JSONB) — portable to SQLite for tests; Postgres stores JSONB
    # via the dialect's JSON type adapter.
    labels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class WorkerInstallTokenRow(Base):
    """The single active install token per workspace.

    Re-minting replaces the prior row (``workspace_id`` is unique), so a
    workspace has at most one usable install token at a time. Only the
    SHA-256 hash is persisted — the plaintext is returned once at mint time
    and never stored.
    """

    __tablename__ = "executor_install_tokens"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_executor_install_tokens_workspace"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "ExecutorsBase",
    "WorkerInstallTokenRow",
    "WorkerRow",
]
