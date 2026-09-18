"""Postgres Row-Level Security — defense layer 3 (Workflow §2.3).

The active workspace is published into the Postgres GUC
``app.current_workspace_id`` so the RLS policy installed by
the ``gdpr_l1_and_rls`` alembic migration can enforce workspace isolation at
the database itself — defense-in-depth on top of the request-context
contextvar (layer 1) and the global ORM auto-filter (layer 2). A compromised
app server that bypasses the ORM filter still cannot read another
workspace's row because the database refuses to return it.

Publication is per TRANSACTION, from the contextvar
-------------------------------------------------
:func:`install_workspace_guc_listener` registers a ``Session.after_begin``
listener — the layer-3 sibling of layer 2's ``do_orm_execute`` listener — that
republishes :data:`backend.data.scoping.current_workspace_id` into the GUC at
the start of every transaction, and it publishes it ``is_local=true`` so the
database drops it again at commit/rollback.

Both halves of that were measured on a probe PG against the previous
``is_local=false``, publish-once-per-request design (#959):

* **residue** — the value outlived the transaction on a POOLED connection, so
  the next checkout inherited it. That is not merely untidy: the queue poller's
  claim query is deliberately workspace-less and RLS-fail-open, so an inherited
  GUC makes it fail CLOSED and runs stop being claimed for every other
  workspace. (Same mechanism as the intermittent-signup failure argued in #959
  §4: ``POST /api/auth/login`` INSERTs a workspace on whatever connection it
  gets.)
* **evaporation** — a Session releases its connection at ``commit()``, so from
  the second turn of a drive onward the session was on a DIFFERENT connection
  with no GUC at all. Layer 3 was silently off for the rest of the run.

A per-transaction publication has neither failure mode, and it needs no call
site: any code inside :func:`backend.data.scoping.workspace_scope` is covered,
which is what gives the background paths (workers, schedules, delivery) a layer
3 they never had.

SQLite has no GUCs; the listener and both helper variants are a NO-OP on that
backend so unit tests don't blow up. The migration similarly skips its
``ENABLE ROW LEVEL SECURITY`` + policy DDL on SQLite (alembic dialect probe).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from backend.data.scoping import current_workspace_id, workspace_scope

_GUC_NAME = "app.current_workspace_id"


def _is_pg(bind: Connection | AsyncConnection) -> bool:
    """Detect Postgres via the bind's dialect name."""
    return bind.dialect.name == "postgresql"


async def set_workspace_guc(conn: AsyncConnection, workspace_id: uuid.UUID) -> None:
    """Set ``app.current_workspace_id`` for the current PG session.

    No-op on non-PG dialects (SQLite tests). Uses ``set_config(name, value,
    is_local=true)``: the GUC lives for THIS transaction only, so it can never
    ride a pooled connection into the next caller's transaction (see the module
    docstring for the measurement). Subsequent transactions are re-armed by
    :func:`install_workspace_guc_listener`.

    Still worth calling explicitly where the workspace is resolved mid-request:
    the session's transaction has usually already begun by then (resolution
    itself reads ``memberships``), so ``after_begin`` has already fired with an
    empty contextvar and only this call covers the rest of that transaction.
    """
    if not _is_pg(conn):
        return
    await conn.execute(
        text(f"SELECT set_config('{_GUC_NAME}', :value, true)"),
        {"value": str(workspace_id)},
    )


async def clear_workspace_guc(conn: AsyncConnection) -> None:
    """Return the GUC to the fail-OPEN empty value for the rest of the txn.

    The policy reads ``current_setting(guc, true) IS NULL OR = '' OR col = it``,
    so an EMPTY guc is permissive and a guc set to the WRONG workspace is
    fail-CLOSED. That asymmetry is why this exists: a loop that publishes
    workspace A and then queries workspace B **in the same transaction** gets
    zero rows back — silently, as a wrong count rather than an error.

    Measured 2026-09-18: without this, the daily-brief loop reported the second
    workspace's shipped count as 0 on PostgreSQL (CI), while SQLite — which has
    no RLS at all — stayed green locally.
    """
    if not _is_pg(conn):
        return
    await conn.execute(text(f"SELECT set_config('{_GUC_NAME}', '', true)"))


def set_workspace_guc_sync(conn: Connection, workspace_id: uuid.UUID) -> None:
    """Sync counterpart of :func:`set_workspace_guc`.

    Also the primitive :func:`_publish_workspace_guc` uses: ``after_begin``
    hands out a sync :class:`Connection` even for an async session.
    """
    if not _is_pg(conn):
        return
    conn.execute(
        text(f"SELECT set_config('{_GUC_NAME}', :value, true)"),
        {"value": str(workspace_id)},
    )


def _publish_workspace_guc(
    session: Session,  # noqa: ARG001 — event signature
    transaction: SessionTransaction,  # noqa: ARG001 — event signature
    connection: Connection,
) -> None:
    """``after_begin`` hook — arm layer 3 for the transaction just opened.

    Runs for EVERY session, sync or async (async sessions dispatch listeners
    inside the greenlet, so the sync ``execute`` here is correct). When no
    workspace is bound the hook does nothing at all — a workspace-less path
    (the queue poller's claim, alembic, boot scripts) stays fail-open exactly
    as before and pays no extra round trip.
    """
    workspace_id = current_workspace_id.get()
    if workspace_id is None:
        return
    set_workspace_guc_sync(connection, workspace_id)


@asynccontextmanager
async def workspace_session_scope(
    session: AsyncSession, workspace_id: uuid.UUID
) -> AsyncIterator[None]:
    """Publish ``workspace_id`` to BOTH layers for this session's live txn.

    :func:`backend.data.scoping.workspace_scope` alone sets only the contextvar
    (layer 2). Layer 3's GUC is armed by the ``after_begin`` listener, which
    fires ONCE per transaction — so a loop that opens its transaction before the
    first iteration (any ``async with session_factory()`` wrapping a ``for``)
    has already armed it with an EMPTY workspace and never re-arms.

    Empty is fail-open, so that alone is only a missing guard. The damage is on
    the NEXT iteration: once any transaction re-begins while workspace A is
    scoped, workspace B's queries in that transaction match a GUC of A and come
    back **empty** — a wrong count, not an error.

    So this publishes the GUC explicitly (the same thing ``api/deps.py`` and
    ``mcp/server.py`` do after resolving a request's workspace) and clears it on
    the way out, leaving the transaction fail-open for whatever runs next.
    """
    with workspace_scope(workspace_id):
        conn = await session.connection()
        await set_workspace_guc(conn, workspace_id)
        try:
            yield
        finally:
            await clear_workspace_guc(await session.connection())


_listener_installed = False


def install_workspace_guc_listener() -> None:
    """Register the ``after_begin`` listener once, process-wide."""
    global _listener_installed  # noqa: PLW0603 — install-once guard, mirrors scoping.py
    if _listener_installed:
        return
    event.listen(Session, "after_begin", _publish_workspace_guc)
    _listener_installed = True


# Register on import so layer 3 follows layer 2 with no per-call-site wiring.
# Idempotent via the module-level guard (same shape as
# ``backend.data.scoping.install_workspace_filter``).
install_workspace_guc_listener()


__all__ = [
    "install_workspace_guc_listener",
    "set_workspace_guc",
    "set_workspace_guc_sync",
]
