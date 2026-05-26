"""Fresh-PG alembic round-trip — gated on ``BSVIBE_DATABASE_URL`` reachability.

Runs the full migration chain against a live Postgres + pgvector
instance:

1. ``alembic upgrade head`` — every revision applies on a clean DB.
2. The expected head revision (``resource_bindings``) is stamped
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
    return os.environ.get(
        "BSVIBE_FRESH_PG_URL",
        os.environ.get("BSVIBE_DATABASE_URL", get_settings().database_url),
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
    env_extra = {"BSVIBE_DATABASE_URL": url}

    # Clean slate so the test is idempotent regardless of prior state.
    asyncio.run(_drop_everything(url))

    # Phase 1 — fresh upgrade.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    stamped = asyncio.run(_stamped_head(url))
    assert stamped == "compensation_wiring", f"expected head compensation_wiring, got {stamped}"

    # Phase 2 — full downgrade. Verifies every revision's downgrade path.
    _alembic(["downgrade", "base"], env_extra=env_extra)

    # Phase 3 — re-upgrade. Verifies the chain is idempotent.
    _alembic(["upgrade", "head"], env_extra=env_extra)
    stamped = asyncio.run(_stamped_head(url))
    assert stamped == "compensation_wiring"


def test_model_account_api_key_encrypted_is_nullable_after_upgrade():
    """Lift 5a migration makes ``model_accounts.api_key_encrypted`` NULLABLE so
    executor accounts (which carry no api key) can be inserted with a NULL."""
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_DATABASE_URL": url}

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


def test_pgvector_extension_installed_after_upgrade():
    """The 1.5b migration's CREATE EXTENSION must actually land.

    Without this, ``intent_examples.embedding`` columns would emit
    ``type "vector" does not exist`` at insert time — silently passing
    unit tests (SQLite path), exploding on first prod insert.
    """
    url = _skip_if_no_pg()
    env_extra = {"BSVIBE_DATABASE_URL": url}

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
