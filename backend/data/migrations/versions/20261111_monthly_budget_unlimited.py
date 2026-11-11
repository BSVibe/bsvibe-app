"""월 토큰 예산 기본값을 무제한(NULL)으로 — 토큰은 BYO.

2026-10-16 결정: 모델 토큰은 BYO 라 플랫폼이 막을 이유가 없다. 월 예산은 사용자가 고르는
지출 상한으로만 남는다. 컬럼의 ``server_default`` 를 없애고, 기존 워크스페이스 중 값이 정확히
50,000,000(옛 기본값)인 행만 NULL 로 바꾼다. 다른 값은 누군가 정한 것이므로 건드리지 않는다.

downgrade 는 server_default 를 50,000,000 으로 되돌리고 NULL 을 50,000,000 으로 채운다.

Revision ID: monthly_budget_unlimited
Revises: safe_mode_approved_delivered
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "monthly_budget_unlimited"
down_revision = "safe_mode_approved_delivered"
branch_labels = None
depends_on = None

_OLD_DEFAULT = 50_000_000


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.alter_column("monthly_token_budget", server_default=None)
    op.execute(
        sa.text(
            "UPDATE workspaces SET monthly_token_budget = NULL WHERE monthly_token_budget = :old"
        ).bindparams(old=_OLD_DEFAULT)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE workspaces SET monthly_token_budget = :old WHERE monthly_token_budget IS NULL"
        ).bindparams(old=_OLD_DEFAULT)
    )
    with op.batch_alter_table("workspaces") as batch:
        batch.alter_column("monthly_token_budget", server_default=str(_OLD_DEFAULT))
