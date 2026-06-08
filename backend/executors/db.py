"""SQLAlchemy schema for the external executor-worker registration subsystem.

Lift 1 of the executor-pool epic — the registration model ported from
BSGateway and adapted to monorepo conventions (SQLAlchemy + alembic,
``workspace_id`` as the tenancy axis, JSON columns portable to the SQLite test
tier).

An *external* worker is a remote machine that runs CLI executors
(``claude_code`` / ``codex`` / ``opencode``). It authenticates to the backend
with an opaque per-worker token (only the SHA-256 hash is stored). A worker
is bootstrapped using the host's OAuth credential (Lift E4 — the CLI sends
``Authorization: Bearer <token>`` to ``POST /api/v1/workers/register``).

Distinct from :mod:`backend.workers.db` (the Bundle G internal-daemon liveness
model, table ``workers``). That subsystem tracks the orchestrator's own
consumer-group daemons; this one tracks externally-installed executor hosts.
The name ``workers`` is already taken there, so this subsystem owns its own
table: ``executor_workers``.

Lift E5 (2026-06-06) — the legacy ``executor_install_tokens`` table and its
``WorkerInstallTokenRow`` ORM are gone. The host now registers with its
OAuth bearer; there is no install-token paste step.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
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
    # Lift E16 — capacity-aware dispatch. The worker stamps its
    # ``len(in_flight)`` here on every heartbeat. ``find_available_worker``
    # excludes rows where ``last_in_flight >= max_parallel_tasks_per_worker``
    # so the backend stops dispatching onto a worker stream a worker has
    # paused polling (its poll loop skips polling when at-cap). Nullable +
    # default 0 for back-compat with pre-E16 worker shapes: a NULL value is
    # treated as "no signal — let it through" so a stale-shape worker is
    # never capacity-excluded just because it never reported.
    last_in_flight: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ExecutorTaskRow(Base):
    """One row per executor task — the dispatch unit (Lift 2).

    A task is created ``pending``, dispatched to a worker's Redis Stream
    (``status=dispatched`` + ``worker_id`` set), then closed ``done`` / ``failed``
    when the worker reports a result. The DB row is the source of truth; the
    Redis Stream entry is only the dispatch notification and the
    ``task:{id}:done`` pub/sub message only a wake-up — the awaiter always reads
    the canonical terminal state from this row.

    Mirrors BSGateway's ``executor_tasks`` table, re-tenanted on ``workspace_id``
    (BSGateway uses ``tenant_id``). ``prompt`` / ``system`` / ``output`` are
    ``Text`` (unbounded); ``system`` / ``workspace_dir`` carry the executor's
    invocation context (forwarded verbatim onto the dispatch stream).
    """

    __tablename__ = "executor_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # The ExecutionRun this task belongs to (executor-pool Lift 5b / B1). Nullable
    # for back-compat with substrate-only tasks created without a run binding; set
    # by the ExecutorOrchestrator so the result path can resolve the run workspace
    # (``run_workspace_root/<run_id>/``) to persist the files the CLI produced.
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # Nullable until dispatched; indexed so a worker can scan its own queue and
    # find_available_worker / the dispatch worker can join by assignment.
    worker_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    executor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    system: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_dir: Mapped[str] = mapped_column(String(1024), nullable=False, default=".")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Relative paths (within the run workspace) of the files the worker produced
    # and the backend persisted (B1). NULL until a result with files is recorded;
    # surfaced as the Deliverable's ``artifact_refs`` by the orchestrator. JSON
    # (not JSONB) for SQLite test-tier portability, matching the other executor
    # tables' JSON columns.
    artifact_refs: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


__all__ = [
    "ExecutorTaskRow",
    "ExecutorsBase",
    "WorkerRow",
]
