"""w1_workspace_cleanup — retire ship_or_discard + reset e2e-hello.

W1 lift (worktree-based product workspace) replaces L-P2's
``ship_or_discard`` Decision model with an auto-merge approach (W2 wires
the actual merge). Pending L-P2 Decisions are now un-resolvable (their
action keys no longer exist in the resolver map), so we delete them.

The e2e-hello reality audit (2026-05-27) seeded test data that doesn't
fit the new model: 3 runs with NULL product binding, 2 verified
deliverables with the same ``hello.py`` filename across separate
sandboxes. Per the founder, this is test data — clear it and start
fresh under W1's worktree model. Other products' data is untouched.

Idempotent: re-runs are no-ops (the rows it deletes are gone).
Postgres-only — SQLite test scaffolds use ``create_all`` not this
migration, and tests do not seed e2e-hello.

Revision ID: w1_workspace_cleanup
Revises: backfill_ship_or_discard
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "w1_workspace_cleanup"
down_revision: Union[str, Sequence[str], None] = "backfill_ship_or_discard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite tests don't hit this migration; they use create_all

    # 1. Retire pending L-P2 ship_or_discard Decisions. The resolver map
    #    no longer has actions for this kind, so these would 400 on any
    #    founder click. Mark them resolved with an audit-friendly reason
    #    so the Decisions tab doesn't keep showing dead rows.
    op.execute(
        sa.text(
            """
            UPDATE execution_decisions
            SET status = 'resolved',
                resolution = 'retired_by_w1_lift',
                resolved_at = NOW()
            WHERE decision = 'ship_or_discard'
              AND status = 'pending'
            """
        )
    )

    # 2. Reset e2e-hello product's data. ProductRow is preserved (so the
    #    same slug stays bound to the same workspace); only the runs +
    #    their children get deleted. FK CASCADE handles the cascade onto
    #    deliverables / work_steps / execution_decisions / etc.
    op.execute(
        sa.text(
            """
            DELETE FROM execution_runs er
            USING products p
            WHERE er.product_id = p.id
              AND p.slug = 'e2e-hello'
            """
        )
    )
    # The trigger_events for e2e-hello runs are tied via request_id chain
    # but not via execution_runs FK; clean them separately by product_id.
    op.execute(
        sa.text(
            """
            DELETE FROM requests r
            USING trigger_events te, products p
            WHERE r.trigger_event_id = te.id
              AND te.product_id = p.id
              AND p.slug = 'e2e-hello'
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM trigger_events te
            USING products p
            WHERE te.product_id = p.id
              AND p.slug = 'e2e-hello'
            """
        )
    )


def downgrade() -> None:
    # Deleting data is not reversible. Downgrade is a no-op (the rows are
    # gone). The L-P2 ship_or_discard rows could in principle be flipped
    # back to pending, but they would still be un-resolvable until the
    # code is reverted alongside.
    pass
