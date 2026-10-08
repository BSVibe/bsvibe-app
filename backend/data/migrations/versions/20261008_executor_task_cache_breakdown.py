"""executor_tasks.usage_{input,cache_read,cache_write_5m,cache_write_1h}_tokens (#1104).

실측 2026-09-30: 런 ``92b76fba`` 의 Claude Code 태스크가 입력 3,055,575 로 한 숫자만
남겼다 — ``input + cache_creation + cache_read`` 원시 합. 이후 가중치(캐시 읽기 0.1,
쓰기 1.25× / 2×)와 세션 내 예산은 들어갔지만, 행에는 여전히 가중 합 하나뿐이라
사후에 실제 비용을 다시 계산할 수도, 틀린 가중치를 다시 적용할 수도 없다.

이 네 컬럼이 그 원시 분해다. ``usage_prompt_tokens`` 와 같은 타입·기본값:
``BigInteger`` NOT NULL server_default 0 — 이 컬럼보다 앞선 태스크와 필드를 모르는
옛 워커가 닫는 태스크는 0(미측정)으로 읽힌다.

Revision ID: executor_task_cache_split
Revises: rls_fail_closed
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "executor_task_cache_split"
down_revision = "rls_fail_closed"
branch_labels = None
depends_on = None

_COLUMNS = (
    "usage_input_tokens",
    "usage_cache_read_tokens",
    "usage_cache_write_5m_tokens",
    "usage_cache_write_1h_tokens",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "executor_tasks",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("executor_tasks", name)
