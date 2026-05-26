"""Postgres Row-Level Security — defense layer 3 (Workflow §2.3).

The :func:`set_workspace_guc` helper publishes the active workspace into the
per-session GUC ``app.current_workspace_id`` so the RLS policy installed by
the ``gdpr_l1_and_rls`` alembic migration can enforce workspace isolation at
the database itself — defense-in-depth on top of the request-context
contextvar (layer 1) and the global ORM auto-filter (layer 2). A compromised
app server that bypasses the ORM filter still cannot read another
workspace's row because the database refuses to return it.

SQLite has no GUCs; both the sync and async helper variants are a NO-OP on
that backend so unit tests don't blow up. The migration similarly skips its
``ENABLE ROW LEVEL SECURITY`` + policy DDL on SQLite (alembic dialect probe).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

_GUC_NAME = "app.current_workspace_id"


def _is_pg(bind: Connection | AsyncConnection) -> bool:
    """Detect Postgres via the bind's dialect name."""
    return bind.dialect.name == "postgresql"


async def set_workspace_guc(conn: AsyncConnection, workspace_id: uuid.UUID) -> None:
    """Set ``app.current_workspace_id`` for the current PG session.

    No-op on non-PG dialects (SQLite tests). Uses ``set_config(name, value,
    is_local=false)`` — the GUC sticks for the lifetime of the connection so
    the SQLAlchemy connection pool can reuse it; the per-request middleware
    rewrites it on every check-out.
    """
    if not _is_pg(conn):
        return
    await conn.execute(
        text(f"SELECT set_config('{_GUC_NAME}', :value, false)"),
        {"value": str(workspace_id)},
    )


def set_workspace_guc_sync(conn: Connection, workspace_id: uuid.UUID) -> None:
    """Sync counterpart of :func:`set_workspace_guc` (for alembic / scripts)."""
    if not _is_pg(conn):
        return
    conn.execute(
        text(f"SELECT set_config('{_GUC_NAME}', :value, false)"),
        {"value": str(workspace_id)},
    )


__all__ = ["set_workspace_guc", "set_workspace_guc_sync"]
