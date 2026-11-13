"""github 연결의 설치 참조를 서버 claim 기록과 맞춘다(1회 정리).

#1368 이전에는 커넥터 생성 경로에서 ``external_ref`` 를 임의로 넣을 수 있었다. 설치 claim 흐름은
``external_ref`` 를 기록하면서 같은 계정에 github OAuth 토큰 행(``connector_oauth_tokens``)을
남기므로, 그 행이 없는 github 계정의 숫자 ``external_ref`` 는 서버가 쓴 값이 아니다.
그런 값만 NULL 로 비우고 로그로 남긴다. claim 기록이 있는 행과 숫자가 아닌 값은 건드리지 않는다.

downgrade 는 비운 값을 복구할 수 없으므로 아무것도 하지 않는다.

Revision ID: github_installation_ref_cleanup
Revises: monthly_budget_unlimited
Create Date: 2026-11-13
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "github_installation_ref_cleanup"
down_revision = "monthly_budget_unlimited"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def clear_unverified_installation_refs(conn: sa.Connection) -> list[str]:
    """claim 기록과 일치하지 않는 github 숫자 ``external_ref`` 를 비우고 비운 계정 id 를 돌려준다."""
    rows = conn.execute(
        sa.text(
            "SELECT a.id, a.external_ref FROM connector_accounts a "
            "WHERE a.connector = 'github' AND a.external_ref IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM connector_oauth_tokens t "
            "WHERE t.connector_account_id = a.id AND t.provider = 'github')"
        )
    ).all()
    cleared: list[str] = []
    for account_id, ref in rows:
        if not ref.strip().isdigit():
            continue
        conn.execute(
            sa.text("UPDATE connector_accounts SET external_ref = NULL WHERE id = :id").bindparams(
                id=account_id
            )
        )
        logger.warning(
            "github installation ref cleared: account=%s external_ref=%s (no install claim record)",
            account_id,
            ref,
        )
        cleared.append(str(account_id))
    return cleared


def upgrade() -> None:
    clear_unverified_installation_refs(op.get_bind())


def downgrade() -> None:
    """비운 값은 복구할 수 없다."""
