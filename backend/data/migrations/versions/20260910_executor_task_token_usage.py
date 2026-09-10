"""executor_tasks.usage_{prompt,completion}_tokens — the executor turn's meter.

게이트 1 후속 (2026-09-10). #911 이 런별 토큰 계측과 런어웨이 상한
(``agent_max_run_tokens``, 2M)을 넣었지만, 그 계측은 **네이티브 LiteLLM 턴**
하나에만 배선돼 있었다. 등록형 호스트 워커가 실행하는 런(코딩 에이전트 CLI —
``claude_code`` / ``codex`` / ``opencode``)은 체인의 네 링크가 전부 비어 있어
**영원히 0 토큰**을 기록했고, 따라서 상한이 **한 번도 발화할 수 없었다** — 정확히
그 상한이 존재하는 이유인 런어웨이 종류(CLI 에이전트가 48 work 턴을 도는 것)에.

테넌트는 자기 워커를 등록해야 executor 용량을 얻으므로, 상한은 파운더의 네이티브
런을 뺀 **모든 워크스페이스에서 inert** 였다.

이 두 컬럼이 그 체인의 저장 링크다. 워커가 자기 CLI 스트림에서 읽어 보고한 값을
``record_result`` 가 여기 쓰고, ``ExecutorAdapter`` 가 완료된 이 행에서 읽어
``ChatResponse`` 에 실으면 ``_drive_loop`` 의 천장이 네이티브 턴과 **같은 방식으로**
executor 턴을 잰다.

``execution_runs.usage_*`` (#911)과 타입·기본값을 일치시킨다: ``BigInteger`` —
긴 멀티턴 런은 수백만 토큰을 쓴다. NOT NULL / server_default 0 이라 이 컬럼보다
앞선 태스크는 0(미측정)으로 읽히고, 필드를 모르는 옛 워커가 닫는 태스크도
INSERT/UPDATE 가 값을 안 줘도 된다.

Revision ID: executor_task_tokens
Revises: run_token_usage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "executor_task_tokens"
down_revision = "run_token_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column(
            "usage_prompt_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "executor_tasks",
        sa.Column(
            "usage_completion_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("executor_tasks", "usage_completion_tokens")
    op.drop_column("executor_tasks", "usage_prompt_tokens")
