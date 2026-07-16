"""Alembic env — async-aware, reads BSVIBE_DATABASE_URL from settings.

target_metadata aggregates every declarative base owned by Bundle 1
modules so a single autogenerate covers all of them.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Every module-owned table set registers on the single shared
# ``backend.data.Base``. Importing the module db.py files is what
# populates ``Base.metadata`` — so import them all (for side effects),
# then point Alembic at the one metadata object.
from backend.config import get_settings
from backend.data import Base

# noqa: F401 — imported for table-registration side effects only.
import backend.router.accounts.account_models  # noqa: F401, E402
import backend.router.accounts.models  # noqa: F401, E402
import backend.connectors.db  # noqa: F401, E402
import backend.connectors.auth.db  # noqa: F401, E402
import backend.workflow.infrastructure.delivery.db  # noqa: F401, E402
import backend.workflow.infrastructure.db  # noqa: F401, E402
import backend.executors.db  # noqa: F401, E402
import backend.router.budget.models  # noqa: F401, E402
import backend.embedding.db  # noqa: F401, E402
import backend.router.routing.db  # noqa: F401, E402
import backend.identity.db  # noqa: F401, E402
import backend.workflow.infrastructure.intake.db  # noqa: F401, E402
import backend.schedule.infrastructure.schedule_db  # noqa: F401, E402
import backend.knowledge.canonicalization.db  # noqa: F401, E402
import backend.knowledge.infrastructure.ontology_db  # noqa: F401, E402
import backend.knowledge.ingest.db  # noqa: F401, E402
import backend.knowledge.retrieval.db  # noqa: F401, E402
import backend.notifications.db  # noqa: F401, E402
import backend.router.routing.run_routing.db  # noqa: F401, E402
import plugin.audit.models  # noqa: F401, E402
import backend.workers.db  # noqa: F401, E402
import backend.identity.workspaces_db  # noqa: F401, E402
import backend.identity.oauth_db  # noqa: F401, E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from environment so prod / dev / CI all
# read their own DB endpoint. Migrations run as the OWNER role (B2b two-role
# setup): DDL + CREATE ROLE / policy management need privileges the
# least-privilege runtime role deliberately lacks. ``migration_url()`` falls
# back to ``database_url`` when no separate owner DSN is configured, so
# single-role deployments migrate exactly as before.
config.set_main_option("sqlalchemy.url", get_settings().migration_url())


# One shared metadata now spans every module's tables — the imports
# above registered them all on ``Base.metadata``.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
