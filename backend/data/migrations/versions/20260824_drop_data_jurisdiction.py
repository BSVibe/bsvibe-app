"""강제된 적 없는 ``data_jurisdiction`` 축을 지운다.

형님 판단 2026-08-24: *"없애자. 지금은 굳이 불필요한 기능 같아"*

이 컬럼은 워커 SDK 가 등록 시 신고하면 **저장하고 인덱스만 걸었다** — 모델 소스가
스스로 *"we just store + index it"* 이라 적어뒀고, 이 값으로 분기하는 코드는 **0**이었다.
라우팅 ``ALLOWED_FIELDS`` 에도 없어 룰로 쓸 수 없었고, PWA 의 선택 UI 는 이미 제거돼
있었다(*"invisible infra"*).

게다가 저장 어휘와 표시 어휘가 이미 갈라져 있었다 — prod 에 ``self-hosted-kr`` 이
있는데 응답 Literal 은 5값뿐이라, 500 을 막으려 넣은 관용 변환이 그 값을 조용히
``unknown`` 으로 덮고 있었다.

prod 실측 (2026-08-24): ``unknown`` 8행 · ``self-hosted-kr`` 1행.

``downgrade`` 는 ``20260521_bundle1_initial`` 의 ``upgrade`` 를 그대로 미러한다 —
NOT NULL 이므로 복원 시 서버 기본값 ``'unknown'`` 으로 채운 뒤 기본값을 뗀다.

Revision ID: drop_data_jurisdiction
Revises: drop_dead_worker_tables
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_data_jurisdiction"
down_revision = "drop_dead_worker_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_model_accounts_data_jurisdiction", table_name="model_accounts")
    op.drop_column("model_accounts", "data_jurisdiction")


def downgrade() -> None:
    op.add_column(
        "model_accounts",
        sa.Column("data_jurisdiction", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.alter_column("model_accounts", "data_jurisdiction", server_default=None)
    op.create_index("ix_model_accounts_data_jurisdiction", "model_accounts", ["data_jurisdiction"])
