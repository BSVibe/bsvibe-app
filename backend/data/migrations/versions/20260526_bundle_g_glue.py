"""Bundle G — trigger_events, requests, delivery_events, safe_mode_queue_items,
workers, worker_install_tokens, audit_relay_state.

Workflow §3 + §10.5 + §12.5 #8. Glue layer schemas only — the actual workers /
intake handlers / delivery dispatcher are still skeletons, so no FKs cross
into Bundle X yet (the deliverable_id columns are loose UUIDs).

Revision ID: bundle_g_glue
Revises: bundle_x_execution
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bundle_g_glue"
down_revision: Union[str, Sequence[str], None] = "bundle_x_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_KIND_VALUES = ("webhook", "schedule", "direct", "decision_resolution")
_INTAKE_REQUEST_STATUS_VALUES = (
    "open",
    "running",
    "needs_decision",
    "review_ready",
    "shipped",
    "abandoned",
)
_SAFE_MODE_STATUS_VALUES = ("pending", "approved", "denied", "expired", "extended")
_WORKER_STATUS_VALUES = ("idle", "running", "failed", "dead")

_TRIGGER_KIND = postgresql.ENUM(
    *_TRIGGER_KIND_VALUES, name="intake_trigger_kind_enum", create_type=False
)
_INTAKE_REQUEST_STATUS = postgresql.ENUM(
    *_INTAKE_REQUEST_STATUS_VALUES, name="intake_request_status_enum", create_type=False
)
_SAFE_MODE_STATUS = postgresql.ENUM(
    *_SAFE_MODE_STATUS_VALUES, name="delivery_safe_mode_status_enum", create_type=False
)
_WORKER_STATUS = postgresql.ENUM(
    *_WORKER_STATUS_VALUES, name="workers_worker_status_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in (
        ("intake_trigger_kind_enum", _TRIGGER_KIND_VALUES),
        ("intake_request_status_enum", _INTAKE_REQUEST_STATUS_VALUES),
        ("delivery_safe_mode_status_enum", _SAFE_MODE_STATUS_VALUES),
        ("workers_worker_status_enum", _WORKER_STATUS_VALUES),
    ):
        sa.Enum(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "trigger_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("trigger_kind", _TRIGGER_KIND, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "source",
            "idempotency_key",
            name="uq_trigger_events_ws_src_key",
        ),
    )
    op.create_index("ix_trigger_events_workspace_id", "trigger_events", ["workspace_id"])
    op.create_index("ix_trigger_events_product_id", "trigger_events", ["product_id"])

    op.create_table(
        "requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "trigger_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trigger_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", _INTAKE_REQUEST_STATUS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_requests_workspace_id", "requests", ["workspace_id"])
    op.create_index("ix_requests_ws_status", "requests", ["workspace_id", "status"])
    op.create_index("ix_requests_trigger_event", "requests", ["trigger_event_id"])

    op.create_table(
        "delivery_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deliverable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delivery_events_workspace_id", "delivery_events", ["workspace_id"])
    op.create_index("ix_delivery_events_deliverable", "delivery_events", ["deliverable_id"])

    op.create_table(
        "safe_mode_queue_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deliverable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", _SAFE_MODE_STATUS, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extension_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_safe_mode_queue_items_workspace_id", "safe_mode_queue_items", ["workspace_id"]
    )
    op.create_index(
        "ix_safe_mode_queue_items_ws_status",
        "safe_mode_queue_items",
        ["workspace_id", "status"],
    )
    op.create_index("ix_safe_mode_queue_items_expires", "safe_mode_queue_items", ["expires_at"])

    op.create_table(
        "workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", _WORKER_STATUS, nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workers_workspace_id", "workers", ["workspace_id"])
    op.create_index("ix_workers_ws_status", "workers", ["workspace_id", "status"])

    op.create_table(
        "worker_install_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_worker_install_tokens_hash"),
    )
    op.create_index(
        "ix_worker_install_tokens_workspace_id", "worker_install_tokens", ["workspace_id"]
    )
    op.create_index(
        "ix_worker_install_tokens_ws_account",
        "worker_install_tokens",
        ["workspace_id", "account_id"],
    )

    op.create_table(
        "audit_relay_state",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("last_relayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    for table in (
        "audit_relay_state",
        "worker_install_tokens",
        "workers",
        "safe_mode_queue_items",
        "delivery_events",
        "requests",
        "trigger_events",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for name in (
        "workers_worker_status_enum",
        "delivery_safe_mode_status_enum",
        "intake_request_status_enum",
        "intake_trigger_kind_enum",
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
