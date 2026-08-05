"""#680 — the bootstrap must NOT hold a DB session across the long ingest.

``run_product_bootstrap_job`` wrapped ``run_repo_bootstrap`` — the LLM-heavy,
multi-minute ingest (live log: ``elapsed_ms=118394``) — inside a single
``async with session_factory() as session`` block. Inside that block the real
``_ingest_callable`` runs the account-resolution read on *that same* session
(``resolve_via_caller(session, ...)``), which autobegins a transaction. The
session then sits idle-in-transaction, holding its pooled connection, for the
whole ``compile_batch`` ingest. Postgres' ``idle_in_transaction_session_timeout``
(120s, #633) reaps the connection; the block's ``__aexit__`` rollback then dies
on the closed connection (``InterfaceError``). Because ``mark_status(complete)``
lives OUTSIDE that block, a fully SUCCESSFUL bootstrap could never reach
``complete`` — the product stayed ``ingesting`` forever (live symptom,
BStockReport 2026-08-04; same family as #632/#686).

Proof (mirrors ``test_no_connection_held_inside_verify_long_steps`` for #686): a
pool of exactly ONE connection. The real ``_ingest_callable`` runs with a resolver
that reads the DB (holding a connection) and a parking adapter that stands in for
the minutes ``compile_batch`` spends in the LLM. While it is parked, an
independent concurrent query must still succeed — which is only possible if the
runtime released the resolution session before the ingest.

Real PostgreSQL only: the one-connection QueuePool blocking semantics this asserts
are a PG pool behaviour (the SQLite async pool differs).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.workflow.application.runtime.product_bootstrap_runtime as rt
from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase

from .._support import pg_url, use_real_pg

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


class _ParkingAdapter:
    """A resolved model adapter whose ``chat`` parks on the first call.

    Stands in for the minutes ``compile_batch`` spends awaiting the LLM. It
    opens NO DB session (a LiteLLM-shape account), so the ONLY thing that could
    hold a connection across the park is the runtime's own resolution session —
    the bug under test."""

    def __init__(self, parked: asyncio.Event, release: asyncio.Event) -> None:
        self._parked = parked
        self._release = release

    async def chat(self, *, system: str, messages: list[dict[str, Any]], tools: Any = None):
        del system, messages, tools
        if not self._parked.is_set():
            self._parked.set()
            await self._release.wait()
        return SimpleNamespace(content="{}")


async def test_no_connection_held_across_bootstrap_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not use_real_pg():
        pytest.skip("real Postgres required — SQLite has no QueuePool to exhaust")

    engine = create_async_engine(pg_url(), future=True, pool_size=1, max_overflow=0, pool_timeout=3)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(WorkspacesBase.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)

        workspace_id = uuid.uuid4()
        product_id = uuid.uuid4()
        async with sf() as s:
            s.add(WorkspaceRow(id=workspace_id, name="t", region=_REGION, safe_mode=False))
            await s.flush()
            s.add(
                ProductRow(
                    id=product_id,
                    workspace_id=workspace_id,
                    name="p",
                    slug=f"p-{product_id.hex[:8]}",
                    repo_url="https://x/y",
                )
            )
            await s.commit()

        settings = rt.get_settings()
        product_root = tmp_path / "product_ws"
        product_root.mkdir()
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        object.__setattr__(settings, "product_workspace_root", str(product_root))
        object.__setattr__(settings, "knowledge_vault_root", str(vault_root))
        # Local Ollama is unreachable in CI; the resolver is stubbed below anyway.
        object.__setattr__(settings, "ingest_compile_parallelism", 1)

        # Clone stub: materialise a small repo so the ingest has ≥1 artifact and
        # therefore actually calls the (parking) adapter.
        def _clone_stub(repo_url, dest, *, token=None, depth=1):  # noqa: ANN001, ARG001
            p = Path(dest)
            p.mkdir(parents=True, exist_ok=True)
            (p / "README.md").write_text("# Test project\n\nMeaningful content to ingest.\n")
            subprocess.run(["git", "init", "-q"], cwd=dest, check=True, capture_output=True)

        async def _aclone(*a, **k):
            _clone_stub(*a, **k)

        fake_git = MagicMock()
        fake_git.clone = _aclone

        parked = asyncio.Event()
        release = asyncio.Event()

        async def _reading_resolver(session, **kwargs):  # noqa: ANN001
            # Mimic the production resolver read (line 458): query the session so
            # it checks out a connection + opens a transaction. In the buggy
            # runtime this is the long-lived outer session, so the connection
            # stays parked in an open txn across the compile_batch park below.
            # _ingest_callable references ``resolve_via_caller`` via this runtime
            # module, so patching it here reaches the real ingest closure.
            await session.execute(select(WorkspaceRow.id).limit(1))
            return SimpleNamespace(adapter=_ParkingAdapter(parked, release))

        monkeypatch.setattr(rt, "resolve_via_caller", _reading_resolver)

        job = asyncio.create_task(
            rt.run_product_bootstrap_job(
                product_id=product_id,
                workspace_id=workspace_id,
                repo_url="https://x/y",
                session_factory=sf,
                git_ops=fake_git,
            )
        )
        try:
            await asyncio.wait_for(parked.wait(), timeout=25)

            async def _heartbeat() -> int:
                async with sf() as s:
                    rows = (await s.execute(select(WorkspaceRow.id))).all()
                    return len(rows)

            got = await asyncio.wait_for(_heartbeat(), timeout=5)
            assert got >= 1, "concurrent DB query returned nothing while ingest parked"
        finally:
            release.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(job, timeout=30)

        # The job reached a terminal state — NOT stuck at 'ingesting' (#680).
        async with sf() as s:
            row = await s.get(ProductRow, product_id)
            assert row is not None
            assert row.bootstrap_status != "ingesting", (
                f"bootstrap stuck at 'ingesting' (error={row.bootstrap_error!r}) — "
                f"the completion flip was lost to a reaped connection (#680)."
            )
    finally:
        await engine.dispose()
