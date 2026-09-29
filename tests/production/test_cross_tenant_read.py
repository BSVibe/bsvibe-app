"""[P] #959 — an explicit cross-tenant READ, with writes still closed.

Background workers have to read across tenants: the queue claims, the global
sweeps and the tenant enumerations (11 sites, measured 2026-09-28 with a probe
on the policy). Under the fail-closed policy an EMPTY GUC returns zero rows —
not an error — so those paths would stop in silence.

The decision (#959) is an escape that has to be asked for by name: the GUC value
``'*'`` opens the policy's ``USING`` and nothing else. ``WITH CHECK`` keeps
demanding the row's own workspace, so every WRITE still happens inside that
tenant's scope.

These run as the runtime role (``bsvibe_app``, NOBYPASSRLS) against the migrated
schema — the only place the policy exists.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.scoping import workspace_scope

from .conftest import bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_GUC = "app.current_workspace_id"


async def _two_tenants(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    a = await bootstrap_tenant(factory, supabase_user_id=f"ct-a-{uuid.uuid4()}", email="a@x.io")
    b = await bootstrap_tenant(factory, supabase_user_id=f"ct-b-{uuid.uuid4()}", email="b@x.io")
    return a, b


async def _visible(session: AsyncSession, ids: tuple[uuid.UUID, uuid.UUID]) -> set[uuid.UUID]:
    rows = await session.execute(
        text("SELECT id FROM workspaces WHERE id = ANY(:ids)"), {"ids": list(ids)}
    )
    return {r[0] for r in rows}


async def _guc(session: AsyncSession) -> str:
    return (await session.execute(text(f"SELECT current_setting('{_GUC}', true)"))).scalar_one()


async def test_the_star_guc_opens_reads_across_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _two_tenants(session_factory)
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', '*', true)"))
        assert await _visible(session, ids) == set(ids)


async def test_the_star_guc_does_not_open_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    a, b = await _two_tenants(session_factory)
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', '*', true)"))
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text("UPDATE workspaces SET name = 'hijacked' WHERE id = :id"), {"id": a}
            )


async def test_cross_tenant_read_publishes_the_star_for_transactions_opened_inside(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from backend.data.rls import cross_tenant_read

    ids = await _two_tenants(session_factory)
    with cross_tenant_read():
        async with session_factory() as session:
            assert await _guc(session) == "*"
            assert await _visible(session, ids) == set(ids)
    async with session_factory() as session:
        assert (await _guc(session) or "") == ""


async def test_a_tenant_scope_inside_it_is_the_narrower_one_and_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The claim shape: read ids across tenants, then write each row in its own
    tenant's scope. The write's transaction must carry THAT tenant, not ``*``."""
    from backend.data.rls import cross_tenant_read

    a, _b = await _two_tenants(session_factory)
    with cross_tenant_read(), workspace_scope(a):
        async with session_factory() as session:
            assert await _guc(session) == str(a)


async def test_the_star_guc_does_not_open_deletes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``DELETE`` is checked against ``USING`` only — there is no new row for
    ``WITH CHECK`` to refuse. A policy whose ``USING`` accepts ``'*'`` for every
    command therefore let a cross-tenant read delete every tenant's rows
    (measured 2026-09-29: ``DELETE 1``). The escape is for reading."""
    a, _b = await _two_tenants(session_factory)
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', '*', true)"))
        deleted = await session.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": a})
        assert deleted.rowcount == 0  # type: ignore[attr-defined]
        await session.rollback()


async def test_the_star_guc_does_not_open_inserts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', '*', true)"))
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text(
                    "INSERT INTO workspaces (id, name, created_at, updated_at) "
                    "VALUES (:id, 'x', now(), now())"
                ),
                {"id": uuid.uuid4()},
            )


async def test_the_star_guc_still_lets_a_claim_lock_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``SELECT … FOR UPDATE`` must pass the UPDATE policy's ``USING`` as well as
    SELECT's — the queue claims lock their candidates under ``'*'`` (#1071)."""
    ids = await _two_tenants(session_factory)
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', '*', true)"))
        rows = await session.execute(
            text("SELECT id FROM workspaces WHERE id = ANY(:ids) FOR UPDATE SKIP LOCKED"),
            {"ids": list(ids)},
        )
        assert {r[0] for r in rows} == set(ids)


async def test_the_session_read_puts_back_the_tenant_it_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Used inside an already-scoped transaction (an MCP tool runs under the
    principal's workspace), the block must hand that workspace back — clearing
    to '' would leave the rest of the transaction blind."""
    from backend.data.rls import cross_tenant_session_read

    a, _b = await _two_tenants(session_factory)
    async with session_factory() as session:
        await session.execute(text(f"SELECT set_config('{_GUC}', :v, true)"), {"v": str(a)})
        async with cross_tenant_session_read(session):
            assert await _guc(session) == "*"
        assert await _guc(session) == str(a)
