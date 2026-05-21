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

# Import every base whose tables Alembic should manage. The metadata
# objects are merged below.
from backend.accounts.models import AccountsBase
from backend.config import get_settings
from backend.gateway.budget.models import GatewayBudgetBase
from backend.gateway.embedding.db import GatewayEmbeddingBase
from backend.gateway.routing.db import GatewayRoutingBase
from backend.gateway.rules.db import GatewayRulesBase
from backend.delivery.db import DeliveryBase
from backend.execution.db import ExecutionBase
from backend.intake.db import IntakeBase
from backend.knowledge.canonicalization.db import CanonicalizationBase
from backend.knowledge.ingest.db import IngestBase
from backend.knowledge.retrieval.db import RetrievalBase
from backend.supervisor.audit.models import AuditOutboxBase, SupervisorBase
from backend.workers.db import WorkersBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from environment so prod / dev / CI all
# read their own DB endpoint.
config.set_main_option("sqlalchemy.url", get_settings().database_url)


class _MergedMetadata:
    """Lightweight wrapper so Alembic's autogenerate sees a single
    target_metadata that spans every base's tables."""

    def __init__(self, bases: list) -> None:
        self._bases = bases

    @property
    def tables(self) -> dict:
        merged: dict = {}
        for base in self._bases:
            merged.update(base.metadata.tables)
        return merged

    @property
    def schema(self) -> str | None:
        return None

    @property
    def naming_convention(self) -> dict:
        return {}

    def is_bound(self) -> bool:
        return False


target_metadata = _MergedMetadata(
    [
        AccountsBase,
        GatewayBudgetBase,
        GatewayRulesBase,
        GatewayEmbeddingBase,
        GatewayRoutingBase,
        SupervisorBase,
        AuditOutboxBase,
        CanonicalizationBase,
        IngestBase,
        RetrievalBase,
        ExecutionBase,
        IntakeBase,
        DeliveryBase,
        WorkersBase,
    ]
)


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
