"""Fresh-PG alembic round-trip — gated on ``BSVIBE_DATABASE_URL`` reachability.

Runs the full migration chain against a live Postgres + pgvector
instance:

1. ``alembic upgrade head`` — every revision applies on a clean DB.
2. The expected head revision (``oauth_anonymous_dcr``) is stamped
   in ``alembic_version``.
3. ``alembic downgrade base`` then ``alembic upgrade head`` — verifies
   downgrade paths are reversible (production safety: bad deploy →
   rollback works).

Skipped automatically when PG isn't reachable, so unit-only ``pytest``
runs outside ``docker compose up`` stay green. CI's ``alembic upgrade
head`` step runs the upgrade-only path against a fresh service
container; this test additionally exercises downgrade + re-upgrade.

The CI workflow uses ``pgvector/pgvector:pg16`` so the
``CREATE EXTENSION vector`` in the 1.5b migration finds the
extension installed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings


def _pg_url() -> str:
    # This suite DROPs + recreates the schema and runs alembic — OWNER-role work
    # (B2b two-role setup). Prefer an explicit fresh-PG URL, then the owner
    # migration URL, then the settings owner fallback. The runtime ``bsvibe_app``
    # role cannot do DDL, so never use BSVIBE_DATABASE_URL directly here.
    return os.environ.get(
        "BSVIBE_FRESH_PG_URL",
        os.environ.get("BSVIBE_MIGRATION_DATABASE_URL", get_settings().migration_url()),
    )


async def _pg_reachable(url: str) -> bool:
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _skip_if_no_pg() -> str:
    url = _pg_url()
    if not asyncio.run(_pg_reachable(url)):
        pytest.skip(f"Postgres not reachable at {url}")
    return url


def _alembic(args: list[str], *, env_extra: dict[str, str] | None = None) -> str:
    """Run alembic CLI; return stdout. Raises ``AssertionError`` on non-zero."""
    repo = Path(__file__).parent.parent
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result.stdout


async def _stamped_head(url: str) -> str | None:
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        await engine.dispose()


async def _drop_everything(url: str) -> None:
    """Drop the public schema and recreate it — clean slate for the test.

    pgvector lives in the public schema by default, so the CASCADE drop
    pulls the extension with it. The migration's
    ``CREATE EXTENSION IF NOT EXISTS vector`` reinstalls.
    """
    engine = create_async_engine(url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        await engine.dispose()


def test_fresh_pg_upgrade_round_trip():
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    # Clean slate so the test is idempotent regardless of prior state.
    asyncio.run(_drop_everything(url))

    # Phase 1 — fresh upgrade.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    stamped = asyncio.run(_stamped_head(url))
    assert stamped == "workspace_schedules_instruction", (
        f"expected head workspace_schedules_instruction, got {stamped}"
    )

    # Phase 2 — full downgrade. Verifies every revision's downgrade path.
    _alembic(["downgrade", "base"], env_extra=env_extra)

    # Phase 3 — re-upgrade. Verifies the chain is idempotent.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    stamped = asyncio.run(_stamped_head(url))
    assert stamped == "workspace_schedules_instruction"


def test_notification_channel_keys_renames_email_to_email_sender():
    """Notifier N1a — the matrix ``email`` key is renamed to ``email-sender``.

    Seed a ``notification_prefs`` row carrying a legacy ``"email"`` channel key
    (the pre-N1a hardcoded grid), run the migration, and assert the key is now
    ``"email-sender"`` (the email connector's name) with its value preserved,
    while ``"in_app"`` / ``"slack"`` are untouched. Downgrade restores ``email``.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    # Upgrade to the PARENT so we can seed a pre-N1a-shaped row, then step up.
    _alembic(["upgrade", "runtime_role"], env_extra=env_extra)

    row_id = _uuid.uuid4()
    legacy_matrix = {
        "needs_you": {"in_app": True, "email": True, "slack": False},
        "triggered": {"in_app": True, "email": False, "slack": True},
        "shipped": {"in_app": True, "email": True, "slack": False},
        "failed": {"in_app": True, "email": False, "slack": False},
        "daily_brief": {"in_app": False, "email": True, "slack": False},
    }

    async def _seed() -> None:
        import json as _json

        engine = create_async_engine(url, future=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO notification_prefs "
                        "(id, workspace_id, matrix, quiet_hours_enabled, "
                        " quiet_hours_start, quiet_hours_end, created_at, updated_at) "
                        "VALUES (:id, :ws, CAST(:m AS JSON), false, "
                        " '22:00', '08:00', :now, :now)"
                    ),
                    {
                        "id": row_id,
                        "ws": _uuid.uuid4(),
                        "m": _json.dumps(legacy_matrix),
                        "now": datetime.now(UTC),
                    },
                )
        finally:
            await engine.dispose()

    async def _read_matrix() -> dict:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT matrix FROM notification_prefs WHERE id = :id"),
                        {"id": row_id},
                    )
                ).first()
                return row[0]
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    # Step up over the N1a migration.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    after = asyncio.run(_read_matrix())
    for event, channels in after.items():
        assert "email" not in channels, f"{event} still carries legacy 'email' key"
    assert after["needs_you"]["email-sender"] is True
    assert after["triggered"]["email-sender"] is False
    # Untouched keys survive verbatim.
    assert after["triggered"]["slack"] is True
    assert after["needs_you"]["in_app"] is True

    # Downgrade restores the legacy key.
    _alembic(["downgrade", "runtime_role"], env_extra=env_extra)
    restored = asyncio.run(_read_matrix())
    assert restored["needs_you"]["email"] is True
    assert all("email-sender" not in ch for ch in restored.values())


def test_workspace_timezone_column_round_trips():
    """Notifier N1b — the ``workspaces.timezone`` column adds + drops cleanly.

    Up migration adds a ``VARCHAR(64)`` NOT NULL column with a ``'UTC'`` server
    default (so existing rows backfill in one statement); down migration removes
    it. Both must run without error against a fresh PG so the operational
    rollback path is safe.
    """
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _column_exists() -> bool:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='workspaces' AND column_name='timezone'"
                        )
                    )
                ).first()
                return row is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_column_exists()), "workspaces.timezone column missing after upgrade"

    # Downgrade to the parent revision; column must be gone.
    _alembic(["downgrade", "notification_channel_keys"], env_extra=env_extra)
    assert not asyncio.run(_column_exists()), "workspaces.timezone column survived downgrade"

    # Re-upgrade restores it.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    assert asyncio.run(_column_exists()), "workspaces.timezone column missing after re-upgrade"


def test_run_routing_source_text_column_round_trips():
    """Lift N5 — the ``run_routing_rules.source_text`` column adds + drops cleanly.

    Up migration adds a ``VARCHAR(500)`` NULLable column (the founder's original
    NL condition phrase); down migration removes it. Both must run without error
    against a fresh PG so the operational rollback path is safe.
    """
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _column_exists() -> bool:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='run_routing_rules' "
                            "AND column_name='source_text'"
                        )
                    )
                ).first()
                return row is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_column_exists()), (
        "run_routing_rules.source_text column missing after upgrade"
    )

    # Downgrade to the parent revision; column must be gone.
    _alembic(["downgrade", "drop_layer2_routing_rules"], env_extra=env_extra)
    assert not asyncio.run(_column_exists()), (
        "run_routing_rules.source_text column survived downgrade"
    )

    # Re-upgrade restores it.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    assert asyncio.run(_column_exists()), (
        "run_routing_rules.source_text column missing after re-upgrade"
    )


def test_model_account_api_key_encrypted_is_nullable_after_upgrade():
    """Lift 5a migration makes ``model_accounts.api_key_encrypted`` NULLABLE so
    executor accounts (which carry no api key) can be inserted with a NULL."""
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _insert_null_key() -> bool:
        import uuid as _uuid
        from datetime import UTC, datetime

        engine = create_async_engine(url, future=True)
        try:
            async with engine.begin() as conn:
                # Seed a personal account so the FK-less partition id is real.
                account_id = _uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO accounts (id, workspace_id, label, created_at) "
                        "VALUES (:id, :ws, 'personal', :now)"
                    ),
                    {"id": account_id, "ws": _uuid.uuid4(), "now": datetime.now(UTC)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO model_accounts "
                        "(id, workspace_id, account_id, provider, label, litellm_model, "
                        " api_base, api_key_encrypted, data_jurisdiction, is_active, "
                        " extra_params, created_at, updated_at) "
                        "VALUES (:id, :ws, :acct, 'executor', 'laptop-1', "
                        " 'executor/claude_code', NULL, NULL, 'unknown', true, "
                        " '{}'::jsonb, :now, :now)"
                    ),
                    {
                        "id": _uuid.uuid4(),
                        "ws": _uuid.uuid4(),
                        "acct": account_id,
                        "now": datetime.now(UTC),
                    },
                )
            return True
        finally:
            await engine.dispose()

    assert asyncio.run(_insert_null_key()), (
        "could not insert a NULL api_key_encrypted — column is still NOT NULL"
    )


def test_worker_last_in_flight_column_round_trips():
    """Lift E16 — the ``executor_workers.last_in_flight`` column adds + drops cleanly.

    Up migration adds an ``INTEGER`` column ``NULL`` allowed with ``DEFAULT 0``;
    down migration removes it. Both must run without error against a fresh PG
    so the operational rollback path is safe.
    """
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _column_exists() -> bool:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='executor_workers' "
                            "AND column_name='last_in_flight'"
                        )
                    )
                ).first()
                return row is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_column_exists()), (
        "executor_workers.last_in_flight column missing after upgrade"
    )

    # Downgrade to before this revision; column must be gone. Cannot use
    # ``-1`` because newer revisions (E21+) sit above it now — target the
    # parent revision by name.
    _alembic(["downgrade", "connector_oauth_unclaimed"], env_extra=env_extra)
    assert not asyncio.run(_column_exists()), (
        "executor_workers.last_in_flight column survived downgrade"
    )

    # Re-upgrade restores it.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    assert asyncio.run(_column_exists()), (
        "executor_workers.last_in_flight column missing after re-upgrade"
    )


def test_workspace_schedules_instruction_columns_round_trip():
    """S1 — ``workspace_schedules`` gains kind/payload/title, plugin_name goes
    NULLable, and the old unique constraint is dropped. Both directions must run
    against a fresh PG so the operational rollback path is safe, and an
    ``instruction`` row (NULL plugin_name) must insert after the upgrade."""
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _columns() -> set[str]:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='workspace_schedules'"
                        )
                    )
                ).all()
                return {r[0] for r in rows}
        finally:
            await engine.dispose()

    async def _plugin_name_nullable() -> bool:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name='workspace_schedules' "
                            "AND column_name='plugin_name'"
                        )
                    )
                ).first()
                return row is not None and row[0] == "YES"
        finally:
            await engine.dispose()

    async def _insert_instruction_row() -> bool:
        import uuid as _uuid
        from datetime import UTC, datetime

        engine = create_async_engine(url, future=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO workspace_schedules "
                        "(id, workspace_id, product_id, kind, payload, title, "
                        " plugin_name, cron_expr, next_run_at, last_fired_at, "
                        " enabled, created_at, updated_at) "
                        "VALUES (:id, :ws, NULL, 'instruction', CAST('{\"text\": \"do it\"}' AS JSON), "
                        " NULL, NULL, '0 9 * * 1', :now, NULL, true, :now, :now)"
                    ),
                    {"id": _uuid.uuid4(), "ws": _uuid.uuid4(), "now": datetime.now(UTC)},
                )
            return True
        finally:
            await engine.dispose()

    cols = asyncio.run(_columns())
    assert {"kind", "payload", "title"} <= cols, f"missing S1 columns; got {cols}"
    assert asyncio.run(_plugin_name_nullable()), "plugin_name should be NULLable after upgrade"
    assert asyncio.run(_insert_instruction_row()), "could not insert an NL instruction row"

    # Downgrade to the parent restores NOT NULL plugin_name + the unique
    # constraint and drops the new columns.
    _alembic(["downgrade", "notification_outbox"], env_extra=env_extra)
    cols_after = asyncio.run(_columns())
    assert not ({"kind", "payload", "title"} & cols_after), (
        f"S1 columns survived downgrade; got {cols_after}"
    )

    # Re-upgrade restores them.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    assert {"kind", "payload", "title"} <= asyncio.run(_columns())


def test_pgvector_extension_installed_after_upgrade():
    """The 1.5b migration's CREATE EXTENSION must actually land.

    Without this, ``intent_examples.embedding`` columns would emit
    ``type "vector" does not exist`` at insert time — silently passing
    unit tests (SQLite path), exploding on first prod insert.
    """
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_MIGRATION_DATABASE_URL": url}

    asyncio.run(_drop_everything(url))
    _alembic(["upgrade", "head"], env_extra=env_extra)

    async def _has_vector_ext() -> bool:
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
                return result.first() is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_has_vector_ext()), (
        "pgvector extension not installed after `alembic upgrade head` — "
        "is the CI service container using pgvector/pgvector:pg16?"
    )
