"""배송이 끝난 ``approved`` Safe Mode 항목을 ``delivered`` 로 옮긴다.

``approved → delivered`` 로 넘기는 호출자가 없어, 배송이 성공했는데도(``deliverables.delivered_at``
이 찍힘) 항목이 영원히 ``approved`` 였다. 이제 배송 성공 지점(``delivery_worker._mark_delivered``)이
넘기므로, 그 전에 쌓인 항목만 한 번 옮긴다. 배송이 안 된 ``approved`` 는 건드리지 않는다.

되돌릴 수 없는 데이터 보정이다: downgrade 는 아무것도 하지 않는다(어느 행이 옮겨졌는지 남지 않는다).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "safe_mode_approved_delivered"
down_revision = "merge_watch_ci_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE safe_mode_queue_items SET status = 'delivered' "
            "WHERE status = 'approved' AND deliverable_id IN "
            "(SELECT id FROM deliverables WHERE delivered_at IS NOT NULL)"
        )
    )


def downgrade() -> None:
    pass
