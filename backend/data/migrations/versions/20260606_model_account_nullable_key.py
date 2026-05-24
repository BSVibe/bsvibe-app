"""model_accounts.api_key_encrypted → NULLABLE (executor-pool Lift 5a).

Executor workers are now routing-visible as ``provider='executor'`` model
accounts (BSGateway "abstract the coding agent like an LLM" pattern). An
executor account routes to a CLI worker capability and carries NO api key, so
the ``api_key_encrypted`` column — previously ``NOT NULL`` for real LLM
credentials — must accept ``NULL``.

The downgrade restores ``NOT NULL``. That is only safe BEFORE any executor row
(which stores ``NULL``) exists; a downgrade with executor rows present would
fail on the NOT NULL constraint — acceptable/standard for a clean rollback
before the feature is exercised.

Revision ID: model_account_nullable_key
Revises: executor_tasks
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "model_account_nullable_key"
down_revision: Union[str, Sequence[str], None] = "executor_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "model_accounts",
        "api_key_encrypted",
        existing_type=sa.String(length=1024),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "model_accounts",
        "api_key_encrypted",
        existing_type=sa.String(length=1024),
        nullable=False,
    )
