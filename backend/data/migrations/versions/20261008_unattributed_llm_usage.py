"""unattributed_llm_usage — 런 없는 LLM 호출의 워크스페이스 원장 (#954).

#953 이 런에 집이 있는 채널은 다 봉합했고, 집이 없는 것(지식 ingest — 청크당 1콜,
제품 부트스트랩, settle 추출, 개념 framing·라벨, 라우팅 규칙 컴파일)은
``llm_usage_unattributed`` 로그로만 드러냈다. 실측 2026-10-08: prod 로그 보관은
컨테이너 수명 20개 — 지금 배포 빈도로 약 하루 — 라 "트래픽이 쌓인 뒤 집계"는
할 수 없었다.

형님 결정(2026-10-08): 워크스페이스 원장에 남긴다. 런을 발명해 붙이지 않으면서도
크기를 영구히 잴 수 있고, #928 과금이 ``execution_runs`` 옆에서 그대로 읽는다.

RLS 루트가 아니다 — ``executor_tasks`` 와 같은 레이어 2(ORM 자동 필터) 테이블.

Revision ID: unattributed_llm_usage
Revises: executor_task_cache_split
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "unattributed_llm_usage"
down_revision = "executor_task_cache_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unattributed_llm_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("usage_prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_unattributed_llm_usage_ws_created",
        "unattributed_llm_usage",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_unattributed_llm_usage_ws_created", table_name="unattributed_llm_usage")
    op.drop_table("unattributed_llm_usage")
