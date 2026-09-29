"""Shared helpers for the API suite.

Fixture seeding under the fail-closed RLS policy (#959 ③)
---------------------------------------------------------
The six RLS tables (``workspaces``, ``products``, ``execution_runs``,
``deliverables``, ``execution_decisions``, ``requests``) reject a runtime-role
INSERT whose ``app.current_workspace_id`` GUC does not match the row. A test
that seeds rows with a bare ``s.add(...); await s.commit()`` therefore runs
blind — invisible while the policy was fail-open, a
``new row violates row-level security policy`` error once it is closed.

:func:`flush_per_workspace` is the seeding counterpart of the production write
shape: every pending row is flushed inside :func:`workspace_session_scope` of
ITS OWN workspace (a ``workspaces`` row is its own workspace), so one seed block
can still lay out several tenants — which is exactly what the isolation tests
need. On SQLite the scope is a plain contextvar and the whole thing reduces to
an ordinary flush.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import workspace_session_scope


def _row_workspace(obj: object) -> uuid.UUID | None:
    if getattr(obj, "__tablename__", None) == "workspaces":
        # The id default is Python-side and fires at INSERT time — too late to
        # scope by it, so draw it here (the same ``uuid4`` the default would).
        if obj.id is None:  # type: ignore[attr-defined]
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        return obj.id  # type: ignore[attr-defined]
    return getattr(obj, "workspace_id", None)


async def flush_per_workspace(s: AsyncSession) -> None:
    """Flush the session's pending rows, each under its own workspace's GUC.

    Rows are grouped by workspace in the order first seen; rows that carry no
    workspace at all (users, accounts, …) are not RLS-guarded and go out in a
    final plain flush. Leaves the transaction open — the caller commits.
    """
    groups: dict[uuid.UUID, list[object]] = {}
    for obj in [*s.new, *s.dirty]:
        ws = _row_workspace(obj)
        if ws is not None:
            groups.setdefault(ws, []).append(obj)
    for ws, objs in groups.items():
        async with workspace_session_scope(s, ws):
            await s.flush(objs)
    await s.flush()


async def commit_per_workspace(s: AsyncSession) -> None:
    """:func:`flush_per_workspace`, then commit."""
    await flush_per_workspace(s)
    await s.commit()
