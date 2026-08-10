"""workspaces.verify_stack_slots — concurrent verification-stack budget

A disposable full-surface verification stack is the product's whole deployment
stood up on the founder's machine. The count is bounded for disk safety, but it
lives per workspace rather than as a server constant because it is a plan tier
("N concurrent verifications").

Revision ID: workspace_verify_slots
Revises: oauth_device_codes
Create Date: 2026-08-10
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_verify_slots"
down_revision: Union[str, Sequence[str], None] = "oauth_device_codes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "verify_stack_slots",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "verify_stack_slots")
