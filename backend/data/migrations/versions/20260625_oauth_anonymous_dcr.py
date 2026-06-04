"""oauth_anonymous_dcr — relax oauth_clients.{workspace_id,created_by_user_id} NULL — Lift D2 followup.

Bug 1 of the D2 hotfix: ``claude mcp add`` (and every other MCP client
following the open RFC 7591 DCR convention) cannot reach the founder
auth surface before they hold a token, so the founder-gated
``POST /api/v1/oauth/clients`` endpoint is unreachable for them.

The fix adds a parallel **unauthenticated** ``POST /api/oauth/register``
endpoint that registers loopback-only public clients. Anonymous clients
are NOT bound to a workspace at registration time — the *user* binds the
workspace later, during ``/authorize`` (which DOES run on a real PWA
session). Same logic for ``created_by_user_id``: no authenticated caller,
no user to attribute. Both columns relax to nullable.

Founder-created clients (the v1 Settings UI route) continue to populate
both columns; only anonymous DCR rows carry NULL.

Revision ID: oauth_anonymous_dcr
Revises: oauth_authorization_server
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "oauth_anonymous_dcr"
down_revision: Union[str, Sequence[str], None] = "oauth_authorization_server"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Relax NOT NULL on workspace_id + created_by_user_id so anonymous DCR
    # rows can be inserted before any user / workspace is known. The
    # foreign-key constraints stay — they're harmless on NULL.
    with op.batch_alter_table("oauth_clients") as batch:
        batch.alter_column("workspace_id", nullable=True)
        batch.alter_column("created_by_user_id", nullable=True)


def downgrade() -> None:
    # NB: this will FAIL at runtime if any anonymous DCR rows exist (NULL
    # cells violate the re-tightened NOT NULL). That's intentional — we
    # don't want to silently destroy anonymous DCR rows on a downgrade.
    with op.batch_alter_table("oauth_clients") as batch:
        batch.alter_column("workspace_id", nullable=False)
        batch.alter_column("created_by_user_id", nullable=False)
