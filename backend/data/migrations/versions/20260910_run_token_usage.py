"""execution_runs.usage_{prompt,completion}_tokens — per-run LLM token meter.

게이트 1 (2026-09-10 audit). ``LlmClient.chat`` 은 응답의 usage 토큰을 이미
캡처하고 ``ChatResponse`` 가 그것을 실어 나른다(그 독스트링이 "the agent loop's
token-usage telemetry, say" 로 이 배선을 예견했다). 그런데 ``LoopTurn`` 에서
버려져 아무 데도 안 남았다. 이 두 컬럼이 런별 집계를 담는다 — BYO-key 라 비용은
사용자 프로바이더 청구이므로 목표는 **런어웨이 보호 + 파운더 가시성**이다.

``BigInteger`` — 긴 멀티턴 런은 수백만 토큰을 쓸 수 있다. NOT NULL / server_default
0 이라 과거 런은 0(미측정)으로 읽히고 INSERT 는 값을 안 줘도 된다.

Revision ID: run_token_usage
Revises: drop_producerless_audit_ev
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "run_token_usage"
down_revision = "drop_producerless_audit_ev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column(
            "usage_prompt_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "execution_runs",
        sa.Column(
            "usage_completion_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("execution_runs", "usage_completion_tokens")
    op.drop_column("execution_runs", "usage_prompt_tokens")
