"""Postgres RLS — defense layer 3.

Skipped automatically on SQLite (RLS is a PG-only feature). On a real PG,
the migration ``gdpr_l1_and_rls`` must:

* Enable ROW LEVEL SECURITY on ``workspaces`` and the workspace-scoped
  root tables (``products`` is exercised here).
* Install a policy keyed off the per-session GUC
  ``app.current_workspace_id`` — set via :func:`backend.data.rls.set_workspace_guc`.

With the GUC set to workspace A, a ``SELECT`` against workspace B's row
returns 0 rows (the DB layer blocks even when the ORM filter would not).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests._support import can_reach_pg, migration_pg_url

pytestmark = pytest.mark.asyncio


def _skip_if_no_pg() -> str:
    """Return the OWNER PG URL for DDL/seeding, or skip when PG is unreachable.

    This test drops + recreates the schema, runs alembic, seeds both workspaces
    and mints a fresh non-superuser role — all OWNER-role operations. It uses
    :func:`migration_pg_url` (the ``bsvibe`` owner in the B2b two-role setup); the
    runtime ``bsvibe_app`` role deliberately cannot do any of them. The RLS
    assertion itself runs through a role this test mints, so it proves the policy
    independently of the app runtime role.
    """
    if not os.environ.get("BSVIBE_DATABASE_URL"):
        pytest.skip("BSVIBE_DATABASE_URL not set — RLS test requires real Postgres")
    url = migration_pg_url()
    if not can_reach_pg(url):
        pytest.skip(f"Postgres not reachable at {url}")
    return url


def _alembic_upgrade(url: str) -> None:
    repo = Path(__file__).parent.parent.parent
    env = os.environ.copy()
    # Run alembic as the owner: env.py resolves the URL via migration_url(),
    # which prefers BSVIBE_MIGRATION_DATABASE_URL, then BSVIBE_DATABASE_URL.
    env["BSVIBE_MIGRATION_DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"


async def _drop_everything(url: str) -> None:
    engine = create_async_engine(url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


_TEST_APP_ROLE = "bsvibe_rls_test_app"
_TEST_APP_PASSWORD = "rls_test_pw"  # noqa: S105 — local-only test role


def _swap_role(url: str, role: str, password: str) -> str:
    """Return a copy of ``url`` whose username + password are replaced.

    Hand-built so we don't depend on urllib's URL parser handling
    ``+asyncpg`` style schemes.
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return f"{scheme}://{role}:{password}@{rest}"
    _, hostpart = rest.split("@", 1)
    return f"{scheme}://{role}:{password}@{hostpart}"


async def _ensure_app_role(url: str) -> None:
    """Create a non-superuser, non-BYPASSRLS role + grant it CRUD on test tables.

    Postgres superusers AND roles with ``BYPASSRLS`` ignore row-level policies
    even when ``FORCE ROW LEVEL SECURITY`` is set. The dev/CI Postgres user
    (``bsvibe``) is typically a superuser, which would mask the policy in this
    test. We mint a fresh role with neither attribute, grant it the minimum
    table privileges, and run the cross-workspace assertion through it.
    """
    engine = create_async_engine(url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # Idempotent: drop then create so the password / attributes are
            # always exactly what we expect even on repeat runs.
            await conn.execute(text(f"DROP ROLE IF EXISTS {_TEST_APP_ROLE}"))
            await conn.execute(
                text(
                    f"CREATE ROLE {_TEST_APP_ROLE} LOGIN PASSWORD '{_TEST_APP_PASSWORD}' "
                    "NOSUPERUSER NOBYPASSRLS"
                )
            )
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {_TEST_APP_ROLE}"))
            await conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f"TO {_TEST_APP_ROLE}"
                )
            )
    finally:
        await engine.dispose()


async def test_rls_blocks_cross_workspace_select_at_db_layer() -> None:
    """With the GUC set to workspace A, a SELECT cannot see workspace B's row.

    Runs through a freshly-minted non-superuser role so ``BYPASSRLS`` cannot
    short-circuit the policy. The seeding step intentionally stays on the
    superuser connection (it inserts both workspaces).
    """
    url = _skip_if_no_pg()

    await _drop_everything(url)
    _alembic_upgrade(url)
    await _ensure_app_role(url)

    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    now = datetime.now(UTC)

    # --- Seed via the superuser connection (bypasses RLS) -------------------
    superuser_engine = create_async_engine(url, future=True)
    try:
        async with superuser_engine.begin() as conn:
            for ws_id in (ws_a, ws_b):
                await conn.execute(
                    text(
                        "INSERT INTO workspaces "
                        "(id, name, region, safe_mode, legal_basis, "
                        " created_at, updated_at) "
                        "VALUES (:id, :name, 'us-1', true, 'contract', "
                        " :now, :now)"
                    ),
                    {"id": ws_id, "name": f"ws-{ws_id.hex[:4]}", "now": now},
                )
                await conn.execute(
                    text(
                        "INSERT INTO products "
                        "(id, workspace_id, name, slug, "
                        " created_at, updated_at) "
                        "VALUES (:id, :ws, 'p', :slug, :now, :now)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "ws": ws_id,
                        "slug": f"slug-{ws_id.hex[:4]}",
                        "now": now,
                    },
                )

        # Verify RLS + FORCE both on.
        async with superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'workspaces'"
                    )
                )
            ).first()
            assert row is not None
            assert row[0] is True, "RLS not enabled on workspaces"
            assert row[1] is True, "FORCE RLS not enabled on workspaces"
    finally:
        await superuser_engine.dispose()

    # --- Cross-workspace assertion via the unprivileged app role ------------
    app_engine = create_async_engine(
        _swap_role(url, _TEST_APP_ROLE, _TEST_APP_PASSWORD), future=True
    )
    try:
        async with app_engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_workspace_id', :v, false)"),
                {"v": str(ws_a)},
            )
            visible = (await conn.execute(text("SELECT workspace_id FROM products"))).all()
            ids = {str(row[0]) for row in visible}
            assert str(ws_a) in ids, f"workspace A's product invisible: {ids}"
            assert str(ws_b) not in ids, f"RLS leak — workspace B visible: {ids}"

            # Same gate on the workspaces table itself.
            visible_ws = (await conn.execute(text("SELECT id FROM workspaces"))).all()
            ids_ws = {str(row[0]) for row in visible_ws}
            assert str(ws_a) in ids_ws
            assert str(ws_b) not in ids_ws
    finally:
        await app_engine.dispose()


async def test_set_workspace_guc_helper_runs_on_sqlite_as_noop() -> None:
    """The helper must be a NO-OP on SQLite so unit tests don't blow up."""
    from sqlalchemy import create_engine

    from backend.data.rls import set_workspace_guc_sync

    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.connect() as conn:
        # Should not raise on SQLite (no GUCs).
        set_workspace_guc_sync(conn, uuid.uuid4())


async def test_set_workspace_guc_helper_sets_pg_guc() -> None:
    """On PG the async helper actually issues ``SET LOCAL app.current_workspace_id``."""
    url = _skip_if_no_pg()
    workspace_id = uuid.uuid4()

    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            from backend.data.rls import set_workspace_guc

            await set_workspace_guc(conn, workspace_id)
            row = (
                await conn.execute(text("SELECT current_setting('app.current_workspace_id', true)"))
            ).first()
            assert row is not None and row[0] == str(workspace_id)
    finally:
        await engine.dispose()
