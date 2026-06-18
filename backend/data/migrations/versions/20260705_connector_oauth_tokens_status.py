"""connector_oauth_tokens_status — surface needs_reauth state when refresh dies.

Lift E46. Adds a small status column so the founder-facing layers
(connector list API + PWA card) can tell a healthy OAuth-bound
connector apart from one whose refresh token has been consumed /
revoked / expired and now silently fails every dispatch.

The E45 dogfood retrace caught the gap: a connector with a dead
refresh token still rendered as "connected" in the PWA because the
frontend only reads ``connector_accounts.is_active``. The dispatch
chain (now E45-typed) raises :class:`ConnectorReauthRequired`, but
that's caller-only — the row state never reflected the dead
credential, so the founder had no visible signal to reconnect.

* ``status`` (``VARCHAR(32)``, NOT NULL, default ``'active'``) —
  ``'active'`` for a refresh-able token, ``'needs_reauth'`` when
  :func:`resolve_connector_credentials` caught the refresh failure
  and persisted the signal. The full enum lives in
  :data:`backend.connectors.auth.db.TOKEN_STATUS_VALUES`.

Safe to run online — column carries a NOT NULL DEFAULT so every
existing row immediately backfills to ``'active'``. Down migration
drops the column cleanly.

Revision ID: connector_oauth_tokens_status
Revises: executor_task_repo_url
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_oauth_tokens_status"
down_revision: Union[str, Sequence[str], None] = "executor_task_repo_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connector_oauth_tokens",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("connector_oauth_tokens", "status")
