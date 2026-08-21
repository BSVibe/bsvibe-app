"""쓰인 적 없는 워커 테이블 3개를 지운다 — 등록은 ``executor_workers`` 가 소유한다.

감사 D3. ``workers`` / ``worker_install_tokens`` / ``audit_relay_state`` 는
Bundle G 가 만든 뒤 **한 번도 행이 들어간 적이 없다** (prod 실측 2026-08-21: 세
테이블 모두 0행). 워커 등록의 실제 SoT 는 ``executor_workers`` 다 (prod 8행).

``workers_worker_status_enum`` 도 함께 지운다 — prod 카탈로그에서 사용처가
``workers.status`` 하나뿐임을 확인했다.

``settle_drains`` 는 건드리지 않는다 (prod 130행, settle worker 가 쓴다).

``downgrade`` 의 DDL 은 ``20260526_bundle_g_glue`` 의 ``upgrade`` 를 그대로
미러한 것이다 — 손으로 다시 적으면 드리프트가 난다.

Revision ID: drop_dead_worker_tables
Revises: drop_canon_pg_mirror
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "drop_dead_worker_tables"
down_revision = "drop_canon_pg_mirror"
branch_labels = None
depends_on = None

_WORKER_STATUS_VALUES = ("idle", "running", "failed", "dead")
_WORKER_STATUS = postgresql.ENUM(
    *_WORKER_STATUS_VALUES, name="workers_worker_status_enum", create_type=False
)


def upgrade() -> None:
    for table in ("audit_relay_state", "worker_install_tokens", "workers"):
        op.drop_table(table)
    sa.Enum(name="workers_worker_status_enum").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    _WORKER_STATUS.create(op.get_bind(), checkfirst=True)

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
