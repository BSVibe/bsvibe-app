"""Layer 3 publication — the RLS GUC must follow the layer-2 contextvar.

Measured on a probe Postgres BEFORE this change (pool_size=1, one session):

    A in-txn after set         : <ws>      ← set_workspace_guc worked
    B same session post-commit : <ws>
    C NEXT session (leak?)     : <ws>      ← RESIDUE: the next checkout inherits it

and with a 5-connection pool, the same session across turn-boundary commits:

    turn 0: pid=73 guc=<ws>
    turn 1: pid=70 guc=None                ← layer 3 silently OFF for the rest
    turn 2: pid=71 guc=None                   of the drive

Both follow from ``set_config(..., is_local=false)`` on a POOLED connection:
the value outlives the transaction that set it (residue for whoever checks the
connection out next) while the session that set it moves onto a different
connection at the next commit (no protection where it was wanted).

The fix is to publish per TRANSACTION from the contextvar — a ``do_orm_execute``
sibling: a ``Session.after_begin`` listener issuing ``set_config(..., true)``.
Transaction-local means the database resets it at commit/rollback, so residue
is impossible by construction and every new transaction re-arms it.

⚠️ Residue is not merely untidy here — the queue poller's claim query is
deliberately workspace-less and RLS-fail-open (``compose.prod.yaml:155-157``).
A stale GUC on a pooled connection makes that claim fail CLOSED, which stops
the run pipeline for every other workspace. The RLS-level positive control for
that lives in ``tests/data/test_rls_pg.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.data.scoping import current_workspace_id, workspace_scope

from .._support import pg_url, use_real_pg

pytestmark = pytest.mark.asyncio

_GUC = "app.current_workspace_id"


async def _guc(session) -> str | None:
    return (await session.execute(text(f"SELECT current_setting('{_GUC}', true)"))).scalar()


# ---------------------------------------------------------------------------
# contextvar scope — dialect-independent (runs everywhere)
# ---------------------------------------------------------------------------
async def test_workspace_scope_sets_and_restores_the_contextvar() -> None:
    ws = uuid.uuid4()
    assert current_workspace_id.get() is None
    with workspace_scope(ws):
        assert current_workspace_id.get() == ws
    assert current_workspace_id.get() is None


async def test_workspace_scope_restores_on_exception() -> None:
    """A crashing drive must not leave the next claim scoped to its workspace."""
    ws = uuid.uuid4()
    with pytest.raises(RuntimeError):
        with workspace_scope(ws):
            raise RuntimeError("drive blew up")
    assert current_workspace_id.get() is None


async def test_listener_is_a_noop_on_sqlite() -> None:
    """SQLite has no GUCs — a scoped session must still work (the unit tier)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with workspace_scope(uuid.uuid4()):
            async with sm() as s:
                assert (await s.execute(text("SELECT 1"))).scalar() == 1
                await s.commit()
                assert (await s.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Postgres — the publication mechanics themselves
# ---------------------------------------------------------------------------
def _skip_without_pg() -> None:
    if not use_real_pg():
        pytest.skip("real Postgres required — SQLite has no GUCs")


async def test_guc_is_republished_after_every_commit() -> None:
    """RED before the listener: turn 1+ read ``None`` (a different connection).

    The drive commits at every turn boundary, so a once-per-session publication
    protects only the first turn.
    """
    _skip_without_pg()
    engine = create_async_engine(pg_url(), future=True, pool_size=5, max_overflow=0)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ws = uuid.uuid4()
    # Occupy the other connections so the post-commit checkout is a DIFFERENT
    # one — reproducing what a busy worker actually sees.
    parked = [sm() for _ in range(3)]
    try:
        for p in parked:
            await p.execute(text("SELECT 1"))
        with workspace_scope(ws):
            async with sm() as s:
                await s.execute(text("SELECT 1"))
                await s.commit()
                for p in parked:
                    await p.close()
                parked = []
                for turn in range(3):
                    assert await _guc(s) == str(ws), f"layer 3 off at turn {turn}"
                    await s.commit()
    finally:
        for p in parked:
            await p.close()
        await engine.dispose()


async def test_guc_leaves_no_residue_on_the_pooled_connection() -> None:
    """RED before the listener: the next checkout inherits the workspace id.

    ``pool_size=1`` guarantees the second session gets the SAME physical
    connection the scoped one used.
    """
    _skip_without_pg()
    engine = create_async_engine(pg_url(), future=True, pool_size=1, max_overflow=0)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ws = uuid.uuid4()
    try:
        with workspace_scope(ws):
            async with sm() as s:
                assert await _guc(s) == str(ws)
                await s.commit()
        async with sm() as after:
            assert await _guc(after) in (None, ""), "GUC residue on the pooled connection"
    finally:
        await engine.dispose()


async def test_layer_3_is_installed_in_a_process_that_never_imports_rls() -> None:
    """The detector has to run where the config lives.

    Measured: the worker process's import graph does NOT reach
    ``backend.data.rls`` (only the API's ``deps`` / ``pat_auth`` / MCP server
    do). A listener registered on that module's import would therefore be
    absent in exactly the process #959 is about — layer 2 would arrive in the
    background paths and layer 3 would silently not. Entering
    ``workspace_scope`` must install it regardless of who imported what.

    Runs in a subprocess so the assertion cannot be satisfied by a module this
    test session already imported.
    """
    import asyncio  # noqa: PLC0415
    import sys  # noqa: PLC0415

    script = (
        "import sys, uuid\n"
        "from backend.data.scoping import workspace_scope\n"
        "assert 'backend.data.rls' not in sys.modules, 'premise gone: scoping now imports rls'\n"
        "with workspace_scope(uuid.uuid4()):\n"
        "    pass\n"
        # The load-bearing line: entering the scope is what pulled layer 3 in.
        # Checking ``event.contains`` FIRST would import rls itself and the
        # check could then only ever come out green.
        "assert 'backend.data.rls' in sys.modules, 'workspace_scope did not install layer 3'\n"
        "from sqlalchemy import event\n"
        "from sqlalchemy.orm import Session\n"
        "import backend.data.rls as rls\n"
        "assert event.contains(Session, 'after_begin', rls._publish_workspace_guc), 'layer 3 listener not installed'\n"
        "print('ok')\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, f"stdout={stdout.decode()}\nstderr={stderr.decode()}"
