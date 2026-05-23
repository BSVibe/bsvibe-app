"""Shared test helpers.

``memory_session`` is the one in-memory SQLite session factory every
unit-level conftest builds on. Since Bundle 1's Single Base unification
all module tables register on ``backend.data.Base.metadata``, so a single
``create_all`` materialises whatever models the test module has imported.

The ``pg_url`` / ``can_reach_pg`` / ``db_engine`` helpers are the single
source of truth for the glue/api suites' "SQLite by default, real Postgres
when ``BSVIBE_DATABASE_URL`` is set + reachable" behaviour. They replace a
per-file duplicated probe that was copy-pasted across ~22 modules. Several
copies opened a SQLAlchemy ``+psycopg`` *sync* engine for the reachability
probe, but the ``dev`` extra ships only ``asyncpg`` — so on a stock checkout
that probe ALWAYS raised ``ModuleNotFoundError`` and the suite silently fell
back to in-memory SQLite even with a real PG up. The "real-PG gate" never
actually exercised Postgres, disabling the PG-vs-SQLite drift protection.
``can_reach_pg`` instead does a driver-agnostic TCP connect to the host:port
parsed from the URL, so it cannot rot on a missing sync driver and is
event-loop-safe (callable synchronously inside pytest-asyncio's running loop).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.data import Base

# Default points at the local dev/CI Postgres. Tests only use it when
# ``BSVIBE_DATABASE_URL`` is explicitly set *and* the host:port answers a TCP
# connect — otherwise they run on in-memory SQLite.
_DEFAULT_PG_URL = "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def pg_url() -> str:
    """The configured Postgres URL (``BSVIBE_DATABASE_URL`` or the dev default)."""
    return os.environ.get("BSVIBE_DATABASE_URL", _DEFAULT_PG_URL)


def can_reach_pg(url: str | None = None) -> bool:
    """Driver-agnostic TCP reachability probe for the configured Postgres.

    A plain ``socket.create_connection`` to the host:port parsed from the URL —
    NOT a SQLAlchemy-driver-specific connect. This is deliberate: the original
    per-file probes opened a ``+psycopg`` sync engine (or an async engine then
    relied on it raising), and since only ``asyncpg`` is installed they failed
    on import, masking a live PG as "unreachable". A TCP probe cannot rot on a
    missing sync driver and is safe to call synchronously inside an already
    running event loop (pytest-asyncio fixtures). The real asyncpg connect then
    happens via ``create_async_engine`` in :func:`db_engine`; a failure there
    surfaces loudly rather than masquerading as SQLite.
    """
    import socket  # noqa: PLC0415
    from urllib.parse import urlsplit  # noqa: PLC0415

    parts = urlsplit((url or pg_url()).replace("+asyncpg", ""))
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def use_real_pg() -> bool:
    """True when ``BSVIBE_DATABASE_URL`` is set AND that Postgres is reachable.

    This is the single gate every glue/api fixture consults. When it returns
    ``False`` the suite runs on in-memory SQLite (the CI default for jobs
    without a PG service, and any local checkout without the env var).
    """
    return bool(os.environ.get("BSVIBE_DATABASE_URL")) and can_reach_pg()


async def _clean_all_rows(engine: AsyncEngine) -> None:
    """Child-first row DELETE across every table registered on ``Base.metadata``.

    ``Base.metadata.sorted_tables`` is topologically ordered parent-before-child
    by SQLAlchemy; iterating it in reverse deletes children before parents so FK
    constraints are honoured. We DELETE rather than ``drop_all`` because the
    glue/api modules all share the Single Base metadata, so a real (shared) PG
    carries tables from OTHER suites too — and cross-domain FKs (e.g.
    ``products`` -> ``workspaces``) make a partial ``drop_all`` fail on the
    dependency. Row deletes keep the shared schema stable across repeated runs
    and across suites.

    Each DELETE runs in its OWN transaction: a Postgres transaction that hits an
    error (e.g. a table that this module's ``create_all`` never materialised, or
    a still-referenced parent because another suite left child rows) is marked
    aborted and would poison every subsequent statement in the same transaction.
    Isolating each statement keeps one skip from cascading.
    """
    for table in reversed(Base.metadata.sorted_tables):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DELETE FROM "{table.name}"'))
        except Exception:  # noqa: BLE001 - missing table / residual FK on shared PG
            pass


@asynccontextmanager
async def db_engine(
    *create_metadata: type[DeclarativeBase],
) -> AsyncIterator[tuple[AsyncEngine, bool]]:
    """Yield ``(engine, is_pg)`` for a glue/api suite.

    Picks the real Postgres engine when :func:`use_real_pg` is true, else an
    in-memory SQLite engine. Creates the schema with ``create_all`` (checkfirst
    is on by default, so it is idempotent against a PG that already carries the
    tables). On exit, the PG path does a child-first row DELETE (NOT
    ``drop_all``) so the shared schema survives and cross-metadata FKs don't
    break teardown; the SQLite path needs no cleanup (the in-memory DB dies with
    the engine).

    ``create_metadata`` may name specific declarative bases whose ``.metadata``
    to ``create_all`` (mirroring the per-file ``_BASES`` tuples). When omitted,
    the unified ``Base.metadata`` is used — which, since the Single Base
    unification, materialises every model the test module has imported.
    """
    is_pg = use_real_pg()
    url = pg_url() if is_pg else _SQLITE_URL
    engine = create_async_engine(url, future=True)
    metadatas: list[MetaData] = [b.metadata for b in create_metadata] or [Base.metadata]
    async with engine.begin() as conn:
        for md in metadatas:
            await conn.run_sync(md.create_all)
    if is_pg:
        # Start clean so leftover rows from a prior run / crash can't bleed in.
        await _clean_all_rows(engine)
    try:
        yield engine, is_pg
    finally:
        if is_pg:
            await _clean_all_rows(engine)
        await engine.dispose()


@asynccontextmanager
async def memory_session() -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` bound to a fresh in-memory SQLite engine.

    Creates every table currently registered on ``Base.metadata`` (i.e.
    every model module the caller has imported) and disposes the engine
    on exit.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


def fake_current_user(
    supabase_user_id: str = "test-user", email: str | None = "t@example.com"
) -> Callable[[], object]:
    """Dependency override returning an authenticated authz ``User``.

    Lets API tests satisfy the v1 router-level auth dependency without a real
    JWT. Pair with an explicit ``get_workspace_id`` override (these tests scope
    via that, not membership resolution).
    """
    from backend.shared.authz.types import User

    def _user() -> User:
        return User(id=supabase_user_id, email=email)

    return _user


__all__ = [
    "can_reach_pg",
    "db_engine",
    "fake_current_user",
    "memory_session",
    "pg_url",
    "use_real_pg",
]
