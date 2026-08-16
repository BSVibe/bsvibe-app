"""safe_mode_queue_items — 거절 사유와 결정 주체를 보존한다

형님이 Safe Mode 로 거절할 때 적은 이유가 저장된 적이 없다:

    # safe_mode_queue.py
    del actor_id, reason  # surface for audit hook

그 "audit hook"(``audit_events``)은 prod 에서 **0행**이고, 살아 있는
``audit_outbox``(4,967행)에도 safe-mode 흔적은 0건이다. 테이블에 컬럼도 없었다.
실측 2026-08-16: 거절 91 · 승인 37 — **91번의 "아니오"가 이유째로 사라졌다.**

거절은 가장 값어치 있는 교정이다(redesign §6: agent 가 위반한 표준). ratchet(§5)이
누적되려면 먼저 남아야 한다.

두 컬럼 다 nullable — prod 에 이미 128행이 있고, **과거는 복구 불가**다.
오늘 이후의 결정부터 남는다.

Revision ID: safe_mode_decision_reason
Revises: workspace_verify_slots
Create Date: 2026-08-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "safe_mode_decision_reason"
down_revision: Union[str, Sequence[str], None] = "workspace_verify_slots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "safe_mode_queue_items",
        sa.Column("decided_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "safe_mode_queue_items",
        sa.Column("deny_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("safe_mode_queue_items", "deny_reason")
    op.drop_column("safe_mode_queue_items", "decided_by")
