"""Execution persistence schema — runs, work steps, attempts, deliverables.

Workflow §3 scoping: every per-run entity carries ``workspace_id`` NOT NULL
(usually + ``product_id`` NOT NULL for product-scoped runs). Status enums
mirror :mod:`backend.execution._domain` so the runtime + DB share one source
of truth for the lifecycle vocabulary.

``ExecutionBase`` is an alias of the shared ``backend.data.Base`` — every
module's tables register on one metadata so Alembic autogenerate sees a
single target and cross-module FKs resolve against one registry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

ExecutionBase = Base


# ---------------------------------------------------------------------------
# Status enums (mirror backend.execution._domain — duplicated as StrEnum
# so SQLAlchemy can name a Postgres ENUM)
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    OPEN = "open"
    RUNNING = "running"
    REVIEW_READY = "review_ready"
    SHIPPED = "shipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunAttemptPhase(StrEnum):
    PLANNING = "planning"
    WORKING = "working"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFIED = "verified"
    REJECTED = "rejected"
    FAILED = "failed"


class ProofState(StrEnum):
    UNTESTED = "untested"
    PROVED = "proved"
    REFUTED = "refuted"


class VerificationOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class DeliverableType(StrEnum):
    CODE = "code"
    PR = "pr"
    PAGE = "page"
    PAGE_IMAGE = "page_image"
    DIRECT_OUTPUT = "direct_output"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class ExecutionRun(ExecutionBase):
    """One row per Request that the agent loop is or has been running."""

    __tablename__ = "execution_runs"
    __table_args__ = (
        Index("ix_execution_runs_ws_status", "workspace_id", "status"),
        Index("ix_execution_runs_ws_product", "workspace_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # Logical Request id from Bundle G (intake). FK added in Bundle G's migration.
    request_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(
            RunStatus,
            name="execution_run_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=RunStatus.OPEN,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


class ExecutionRunHistory(ExecutionBase):
    """Append-only status transitions for an :class:`ExecutionRun`."""

    __tablename__ = "execution_run_history"
    __table_args__ = (Index("ix_execution_run_history_run", "run_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    from_status: Mapped[RunStatus | None] = mapped_column(
        SAEnum(
            RunStatus,
            name="execution_run_status_enum",
            create_type=False,
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=True,
    )
    to_status: Mapped[RunStatus] = mapped_column(
        SAEnum(
            RunStatus,
            name="execution_run_status_enum",
            create_type=False,
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class ExecutionRunActivity(ExecutionBase):
    """Telemetry / observability stream for a run (tool calls, events)."""

    __tablename__ = "execution_run_activities"
    __table_args__ = (Index("ix_execution_run_activities_run", "run_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    activity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class CompositionSnapshot(ExecutionBase):
    """Frozen prompt-template + fragment composition per run / step."""

    __tablename__ = "composition_snapshots"
    __table_args__ = (Index("ix_composition_snapshots_run", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    composition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class DecomposerStep(ExecutionBase):
    """One row per CoT decomposer-emitted step."""

    __tablename__ = "decomposer_steps"
    __table_args__ = (Index("ix_decomposer_steps_run_order", "run_id", "order_idx"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    order_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class WorkStep(ExecutionBase):
    """Materialized unit of work the agent picks up + verifies."""

    __tablename__ = "work_steps"
    __table_args__ = (
        Index("ix_work_steps_run", "run_id"),
        Index("ix_work_steps_ws_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[WorkStepStatus] = mapped_column(
        SAEnum(
            WorkStepStatus,
            name="work_step_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=WorkStepStatus.PENDING,
    )
    proof_state: Mapped[ProofState] = mapped_column(
        SAEnum(
            ProofState, name="proof_state_enum", values_callable=lambda ec: [m.value for m in ec]
        ),
        nullable=False,
        default=ProofState.UNTESTED,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


class RunAttempt(ExecutionBase):
    """One agent-loop attempt at a run; multiple per run for retries."""

    __tablename__ = "run_attempts"
    __table_args__ = (Index("ix_run_attempts_run_phase", "run_id", "phase"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    phase: Mapped[RunAttemptPhase] = mapped_column(
        SAEnum(
            RunAttemptPhase,
            name="run_attempt_phase_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=RunAttemptPhase.PLANNING,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Deliverable(ExecutionBase):
    """Produced artifact (PR, page, direct output, …) tied to a run."""

    __tablename__ = "deliverables"
    __table_args__ = (
        Index("ix_deliverables_run", "run_id"),
        Index("ix_deliverables_ws_type", "workspace_id", "deliverable_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    deliverable_type: Mapped[DeliverableType] = mapped_column(
        SAEnum(
            DeliverableType,
            name="deliverable_type_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    artifact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class Decision(ExecutionBase):
    """Approval / direction-change captured during a run."""

    __tablename__ = "execution_decisions"
    __table_args__ = (Index("ix_execution_decisions_run", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class VerificationResult(ExecutionBase):
    """Outcome of a single VerificationContract execution against a run."""

    __tablename__ = "verification_results"
    __table_args__ = (Index("ix_verification_results_run", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    work_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("work_steps.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    outcome: Mapped[VerificationOutcome] = mapped_column(
        SAEnum(
            VerificationOutcome,
            name="verification_outcome_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
