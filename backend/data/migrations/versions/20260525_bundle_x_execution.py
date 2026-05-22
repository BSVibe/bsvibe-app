"""Bundle X — execution runs, history, activities, snapshots, decomposer steps,
work steps, run attempts, deliverables, decisions, verification results.

Workflow §3 scoping: workspace_id NOT NULL everywhere; product_id for runs;
request_id nullable FK (Bundle G will lock it via intake/).

Revision ID: bundle_x_execution
Revises: bundle_k_knowledge
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bundle_x_execution"
down_revision: Union[str, Sequence[str], None] = "bundle_k_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RUN_STATUS_VALUES = ("open", "running", "review_ready", "shipped", "failed", "cancelled")
_RUN_ATTEMPT_PHASE_VALUES = (
    "planning",
    "working",
    "verifying",
    "reviewing",
    "completed",
    "failed",
)
_WORK_STEP_STATUS_VALUES = ("pending", "running", "verified", "rejected", "failed")
_PROOF_STATE_VALUES = ("untested", "proved", "refuted")
_VERIFICATION_OUTCOME_VALUES = ("passed", "failed", "inconclusive")
_DELIVERABLE_TYPE_VALUES = ("code", "pr", "page", "page_image", "direct_output")


_RUN_STATUS = postgresql.ENUM(
    *_RUN_STATUS_VALUES, name="execution_run_status_enum", create_type=False
)
_RUN_ATTEMPT_PHASE = postgresql.ENUM(
    *_RUN_ATTEMPT_PHASE_VALUES, name="run_attempt_phase_enum", create_type=False
)
_WORK_STEP_STATUS = postgresql.ENUM(
    *_WORK_STEP_STATUS_VALUES, name="work_step_status_enum", create_type=False
)
_PROOF_STATE = postgresql.ENUM(*_PROOF_STATE_VALUES, name="proof_state_enum", create_type=False)
_VERIFICATION_OUTCOME = postgresql.ENUM(
    *_VERIFICATION_OUTCOME_VALUES, name="verification_outcome_enum", create_type=False
)
_DELIVERABLE_TYPE = postgresql.ENUM(
    *_DELIVERABLE_TYPE_VALUES, name="deliverable_type_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (
        ("execution_run_status_enum", _RUN_STATUS_VALUES),
        ("run_attempt_phase_enum", _RUN_ATTEMPT_PHASE_VALUES),
        ("work_step_status_enum", _WORK_STEP_STATUS_VALUES),
        ("proof_state_enum", _PROOF_STATE_VALUES),
        ("verification_outcome_enum", _VERIFICATION_OUTCOME_VALUES),
        ("deliverable_type_enum", _DELIVERABLE_TYPE_VALUES),
    ):
        sa.Enum(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "execution_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", _RUN_STATUS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_execution_runs_workspace_id", "execution_runs", ["workspace_id"])
    op.create_index("ix_execution_runs_product_id", "execution_runs", ["product_id"])
    op.create_index("ix_execution_runs_request_id", "execution_runs", ["request_id"])
    op.create_index("ix_execution_runs_ws_status", "execution_runs", ["workspace_id", "status"])
    op.create_index(
        "ix_execution_runs_ws_product", "execution_runs", ["workspace_id", "product_id"]
    )

    op.create_table(
        "execution_run_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", _RUN_STATUS, nullable=True),
        sa.Column("to_status", _RUN_STATUS, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_run_history_workspace_id", "execution_run_history", ["workspace_id"]
    )
    op.create_index(
        "ix_execution_run_history_run", "execution_run_history", ["run_id", "created_at"]
    )

    op.create_table(
        "execution_run_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activity_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_run_activities_workspace_id",
        "execution_run_activities",
        ["workspace_id"],
    )
    op.create_index(
        "ix_execution_run_activities_run",
        "execution_run_activities",
        ["run_id", "created_at"],
    )

    op.create_table(
        "composition_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("composition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_composition_snapshots_workspace_id", "composition_snapshots", ["workspace_id"]
    )
    op.create_index("ix_composition_snapshots_run", "composition_snapshots", ["run_id"])

    op.create_table(
        "decomposer_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_idx", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_decomposer_steps_workspace_id", "decomposer_steps", ["workspace_id"])
    op.create_index("ix_decomposer_steps_run_order", "decomposer_steps", ["run_id", "order_idx"])

    op.create_table(
        "work_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", _WORK_STEP_STATUS, nullable=False),
        sa.Column("proof_state", _PROOF_STATE, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_work_steps_workspace_id", "work_steps", ["workspace_id"])
    op.create_index("ix_work_steps_run", "work_steps", ["run_id"])
    op.create_index("ix_work_steps_ws_status", "work_steps", ["workspace_id", "status"])

    op.create_table(
        "run_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", _RUN_ATTEMPT_PHASE, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_run_attempts_workspace_id", "run_attempts", ["workspace_id"])
    op.create_index("ix_run_attempts_run_phase", "run_attempts", ["run_id", "phase"])

    op.create_table(
        "deliverables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deliverable_type", _DELIVERABLE_TYPE, nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("diff_url", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deliverables_workspace_id", "deliverables", ["workspace_id"])
    op.create_index("ix_deliverables_run", "deliverables", ["run_id"])
    op.create_index("ix_deliverables_ws_type", "deliverables", ["workspace_id", "deliverable_type"])

    op.create_table(
        "execution_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_execution_decisions_workspace_id", "execution_decisions", ["workspace_id"])
    op.create_index("ix_execution_decisions_run", "execution_decisions", ["run_id"])

    op.create_table(
        "verification_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", _VERIFICATION_OUTCOME, nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_verification_results_workspace_id", "verification_results", ["workspace_id"]
    )
    op.create_index("ix_verification_results_run", "verification_results", ["run_id"])


def downgrade() -> None:
    for table in (
        "verification_results",
        "execution_decisions",
        "deliverables",
        "run_attempts",
        "work_steps",
        "decomposer_steps",
        "composition_snapshots",
        "execution_run_activities",
        "execution_run_history",
        "execution_runs",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for name in (
        "deliverable_type_enum",
        "verification_outcome_enum",
        "proof_state_enum",
        "work_step_status_enum",
        "run_attempt_phase_enum",
        "execution_run_status_enum",
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
