"""adversarial_replay_jobs — 재생(Stage A 재실행) 백그라운드 작업의 상태·결과.

재생은 MCP 요청 안에서 끝낼 수 없어(Stage A 한 라운드 180초 > Cloudflare ~100초) 작업으로 등록되고
워커가 실행한다. 상태와 결과는 이 표에 남아 재시작 뒤에도 조회된다. 레이어 2 표(RLS 루트 아님).

Revision ID: adversarial_replay_jobs
Revises: close_terminal_run_decisions
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "adversarial_replay_jobs"
down_revision = "close_terminal_run_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adversarial_replay_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_adversarial_replay_jobs_status_created",
        "adversarial_replay_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_adversarial_replay_jobs_ws_created",
        "adversarial_replay_jobs",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_adversarial_replay_jobs_ws_created", table_name="adversarial_replay_jobs")
    op.drop_index("ix_adversarial_replay_jobs_status_created", table_name="adversarial_replay_jobs")
    op.drop_table("adversarial_replay_jobs")
