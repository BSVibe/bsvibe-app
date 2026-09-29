"""The PG test cleanup deletes every row — whatever the runtime role may see (#959).

``_clean_all_rows`` ran its DELETEs through the engine it was handed: the
runtime role (``bsvibe_app``, NOBYPASSRLS). Under the fail-open policy that
happens to see everything. Under the fail-closed policy (#959 ③) a DELETE with
no workspace GUC matches nothing — no error — and rows pile up between tests
(measured 2026-09-29: production-tier counts of ``36 == 2``).

Cleanup is test infrastructure, not the app, so it runs as the table owner.
The engine below pins a FOREIGN workspace on every connection — the one
condition under which the runtime role sees none of the seeded row today.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from ._support import _clean_all_rows, migration_pg_url, pg_url, use_real_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not use_real_pg(), reason="RLS exists only on the migrated Postgres"),
]


async def test_cleanup_removes_rows_the_runtime_role_cannot_see() -> None:
    ws = uuid.uuid4()
    owner = create_async_engine(migration_pg_url())
    blinkered = create_async_engine(pg_url())

    # ``checkout``, not ``connect``: a pooled connection comes back with its
    # session GUC reset, and ``connect`` fires only for the first checkout — the
    # control query below would silently leave cleanup running with an EMPTY GUC.
    @event.listens_for(blinkered.sync_engine, "checkout")
    def _foreign_workspace(dbapi_conn, _record, _proxy):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute(f"SET app.current_workspace_id = '{uuid.uuid4()}'")
        cur.close()

    try:
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, created_at, updated_at) "
                    "VALUES (:id, 'cleanup-probe', now(), now())"
                ),
                {"id": ws},
            )
        async with blinkered.connect() as conn:  # positive control: it really is blind
            seen = await conn.execute(
                text("SELECT count(*) FROM workspaces WHERE id = :id"), {"id": ws}
            )
            assert seen.scalar_one() == 0

        await _clean_all_rows(blinkered)

        async with owner.connect() as conn:
            left = await conn.execute(
                text("SELECT count(*) FROM workspaces WHERE id = :id"), {"id": ws}
            )
            assert left.scalar_one() == 0
    finally:
        async with owner.begin() as conn:
            await conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})
        await blinkered.dispose()
        await owner.dispose()
