"""터미널 런에 남은 pending 결정을 런을 건드리지 않고 닫는다(1회 정리).

배포된(shipped/cancelled/failed) 런의 대기 결정은 답할 의미가 없다. 런 상태는 바꾸지 않고
결정만 ``resolved`` / ``run_reached_terminal`` 로 닫는다. 끝난 런 위에서 답하는 종류
(``delivery_failed``, ``merge_watch_stalled``)는 제외한다.

downgrade 는 닫힌 결정을 구분할 수 없어 아무것도 하지 않는다.

Revision ID: close_terminal_run_decisions
Revises: github_installation_ref_cleanup
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "close_terminal_run_decisions"
down_revision = "github_installation_ref_cleanup"
branch_labels = None
depends_on = None

_KEEP_KINDS = ("delivery_failed", "merge_watch_stalled")
_TERMINAL = ("shipped", "failed", "cancelled")


def close_decisions_of_terminal_runs(conn: sa.Connection) -> int:
    result = conn.execute(
        sa.text(
            "UPDATE execution_decisions SET status = 'resolved', resolution = 'run_reached_terminal', "
            "resolved_at = now() "
            "WHERE status = 'pending' "
            "AND decision NOT IN :keep "
            "AND run_id IN (SELECT id FROM execution_runs WHERE status IN :terminal)"
        ).bindparams(
            sa.bindparam("keep", expanding=True),
            sa.bindparam("terminal", expanding=True),
        ),
        {"keep": list(_KEEP_KINDS), "terminal": list(_TERMINAL)},
    )
    return int(result.rowcount or 0)


def upgrade() -> None:
    close_decisions_of_terminal_runs(op.get_bind())


def downgrade() -> None:
    """닫힌 결정은 복구할 수 없다."""
