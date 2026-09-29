"""Seeding / verification sessions that publish a workspace (#959 ③).

Under the fail-CLOSED RLS policy a runtime-role session with no workspace GUC
cannot INSERT into the RLS tables and reads ZERO rows from them. A test's own
seeding and after-the-fact verification are not what it is about, so they open
their session here, scoped to the tenant whose rows they touch (a ``workspaces``
row is scoped with its own id). No-op on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import workspace_session_scope


@asynccontextmanager
async def scoped_session(
    factory: Callable[[], AsyncSession], workspace_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """``async with factory() as s`` with ``workspace_id`` published on ``s``."""
    async with factory() as s, workspace_session_scope(s, workspace_id):
        yield s
