"""producer 가 한 번도 없었던 ``audit_events`` 를 지운다.

BSupervisor 에서 lift 될 때 따라온 "모든 AI 에이전트 행위의 비정규화된
조회용 기록"이다. 그런 기록은 실제로 존재하지만 **다른 테이블**에 있다 —
EventBus 재배선(v8 §D5) 이후 producer 는 ``safe_emit`` → ``audit.emit`` →
``audit_outbox`` 로 흐르고, 릴레이가 중앙 싱크로 보낸다. 이 테이블은
아무도 겨누지 않은 채 남아 있었다.

실측 (2026-09-09, 2026-08-16 과 일치):

=========================================================  =====
프로덕션 생성 지점 · select · delete                             0
prod ``audit_events`` 행                                         0
같은 통계 창의 INSERT (대조군 ``audit_outbox`` 는 687)             0
=========================================================  =====

**값의 손실이 없다** — 이 테이블은 살아 있는 동안 단 한 행도 담은 적이 없다.

⚠️ 왜 두 번이나 재발견되고도 살아남았나 — **이름이 셋이기 때문이다.**
``audit_events`` 로 grep 하면 41건이 나오는데 대부분은 현역인
``backend.workflow.application.audit_events`` (이벤트 dataclass) 와
``backend.api.v1.chat_audit_events`` 다. 테이블 쪽은 그 노이즈에 묻혔다.
같은 함정을 이 저장소는 ``WorkerRow`` 로 이미 겪었다
(``20260826_drop_dead_worker_tables``).

⚠️ 그리고 이 자리는 비어 있기만 한 게 아니었다 — #905 까지 **GDPR Art. 30
처리기록의 보존 약속의 주어**였다. 한 번도 바이트를 담은 적 없는 테이블에
"Retained 1 year for security incident review" 를 약속했다. 그 문장은
``audit_outbox`` + 워크스페이스별 ``audit_retention_days``(NULL = forever)
로 정정됐고, 이 마이그레이션은 자리를 지운다.

같은 근거로 이 저장소가 이미 지운 것들: ``routing_logs`` ·
``account_budget_policies`` · 정규화/검색 미러 5개 · ``workers`` /
``worker_install_tokens`` / ``audit_relay_state``.

``downgrade`` 는 ``20260521_bundle1_initial`` 의 ``upgrade`` 를 그대로 미러한다.

Revision ID: drop_producerless_audit_ev
Revises: merge_watch_ci_red_head
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "drop_producerless_audit_ev"
down_revision = "merge_watch_ci_red_head"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_audit_events_event_type_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_agent_id", table_name="audit_events")
    op.drop_table("audit_events")


def downgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=True),
        sa.Column("tenant_id", sa.String(255), nullable=True),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("target", sa.String(1024), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("explanation_json", postgresql.JSONB(), nullable=True),
        sa.Column("feedback_json", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_agent_id", "audit_events", ["agent_id"])
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index(
        "ix_audit_events_workspace_created_at",
        "audit_events",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_event_type_created_at",
        "audit_events",
        ["event_type", "created_at"],
    )
