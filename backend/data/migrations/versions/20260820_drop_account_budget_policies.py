"""예산 정책 테이블을 지운다 — 구조적으로 절대 발화할 수 없던 서브시스템의 마지막 조각.

prod 실측 (2026-08-20): ``account_budget_policies`` **0행**. 행을 만드는 유일한 경로인
``BudgetPolicyRepository.upsert`` 의 프로덕션 호출자가 0이었다 — REST 도, MCP 툴도, PWA
화면도 없었으므로 이 테이블은 영구히 비어 있을 수밖에 없었다.

정책이 없으면 ``check_request_cost`` 는 언제나 ``blocked=False`` 를 돌려주고,
누적을 맡은 트래커 스토어는 요청마다 새로 만들어지는 dict 였다. 두 겹 모두에서
``BudgetExceeded`` 는 발화 불가능했다. 삭제로 동작은 변하지 않는다.

⚠️ 비용 *보고*는 예산 *강제*와 다른 축이고 그대로 남는다 — 프록시 응답의
``bsvibe.actual_cost_cents`` 는 살아 있는 필드다.

전용 ENUM 타입 두 개도 함께 지운다: prod 카탈로그 조회 결과 이 테이블의 두 컬럼
말고는 쓰는 곳이 없어, 테이블만 지우면 고아 타입으로 남는다.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_budget_policies"
down_revision: Union[str, Sequence[str], None] = "drop_gateway_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCOPE = postgresql.ENUM("daily", "monthly", name="budget_scope_enum", create_type=False)
_ENFORCEMENT = postgresql.ENUM(
    "block", "warn", "log", name="budget_enforcement_enum", create_type=False
)


def upgrade() -> None:
    op.drop_index("ix_account_budget_policies_account_id", table_name="account_budget_policies")
    op.drop_index("ix_account_budget_policies_workspace_id", table_name="account_budget_policies")
    op.drop_table("account_budget_policies")
    # The table was the only consumer of both types (verified against the prod
    # catalog), so leaving them would leave orphans behind.
    _ENFORCEMENT.drop(op.get_bind(), checkfirst=True)
    _SCOPE.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    _SCOPE.create(op.get_bind(), checkfirst=True)
    _ENFORCEMENT.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "account_budget_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", _SCOPE, nullable=False),
        sa.Column("cost_cap_cents", sa.Integer(), nullable=False),
        sa.Column("enforcement", _ENFORCEMENT, nullable=False, server_default="block"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "account_id", "scope", name="uq_account_budget_scope"),
    )
    op.create_index(
        "ix_account_budget_policies_workspace_id",
        "account_budget_policies",
        ["workspace_id"],
    )
    op.create_index(
        "ix_account_budget_policies_account_id",
        "account_budget_policies",
        ["account_id"],
    )
