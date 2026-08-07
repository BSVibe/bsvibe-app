"""oauth_access_tokens — ``expires_at`` becomes nullable (PAT groundwork).

A NULL ``expires_at`` means "never expires". Only a Personal Access Token is
issued that way; every grant-issued token (authorization_code, refresh, the
run-scoped executor task token) still writes a concrete expiry.

Widening only — no existing row changes, and every one of them keeps a
non-NULL expiry, so the reverse migration is safe as long as no PAT has been
issued yet. Once PATs exist, ``downgrade()`` would have to decide a lifetime
for rows that deliberately have none; it refuses instead of inventing one.

Revision ID: oauth_token_nullable_expiry
Revises: executor_task_execution_target
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "oauth_token_nullable_expiry"
down_revision: Union[str, Sequence[str], None] = "executor_task_execution_target"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "oauth_access_tokens",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    # Re-imposing NOT NULL would silently need a value for every never-expiring
    # token. Fail loudly instead — the operator decides whether to revoke those
    # PATs or stamp them with an explicit expiry first.
    bind = op.get_bind()
    orphans = bind.execute(
        sa.text("SELECT COUNT(*) FROM oauth_access_tokens WHERE expires_at IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"{orphans} access token(s) have a NULL expires_at (PATs). Revoke them or set "
            "an explicit expires_at before downgrading past "
            "oauth_token_nullable_expiry."
        )
    op.alter_column(
        "oauth_access_tokens",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
