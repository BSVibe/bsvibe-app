"""App DB engine safety-net — ``idle_in_transaction_session_timeout``.

The prod incident: leaked / held-open transactions (sessions left ``idle in
transaction``) accumulated (~15) and exhausted the SQLAlchemy connection pool,
hanging ``/api/v1/workers/heartbeat`` and every DB endpoint into a full outage
that needed a manual backend restart. Post-#632 the drive loop uses SHORT
transactions, so no legit app op holds a transaction idle for more than a few
seconds — which makes it SAFE for Postgres to auto-kill any session left idle
in a transaction past the timeout. This module proves the guard both
(a) computes the right asyncpg ``server_settings`` (unit, no DB), and
(b) is really applied to app connections so PG terminates a leaked idle tx
(the real-PG proof — the point is the LIVE behaviour).
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.data.engine as engine_mod
from backend.data.engine import create_app_engine, idle_tx_server_settings
from tests._support import pg_url, use_real_pg

# asyncio_mode = "auto" (pyproject) runs the lone async test without an explicit
# marker; a module-level ``pytest.mark.asyncio`` would spuriously mark the sync
# unit tests below and emit warnings.

_PG_URL = "postgresql+asyncpg://u:p@h:5432/db"
_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


# --------------------------------------------------------------------------
# Unit — the server_settings the guard computes (no DB needed)
# --------------------------------------------------------------------------
def test_server_settings_present_when_timeout_positive() -> None:
    assert idle_tx_server_settings(_PG_URL, timeout_ms=120000) == {
        "idle_in_transaction_session_timeout": "120000"
    }


def test_server_settings_omitted_when_timeout_zero() -> None:
    # 0 means "disabled" — the guard is a no-op and the GUC is left unset.
    assert idle_tx_server_settings(_PG_URL, timeout_ms=0) == {}


def test_server_settings_omitted_for_non_asyncpg_url() -> None:
    # SQLite has no such GUC — never inject connect_args that aiosqlite rejects.
    assert idle_tx_server_settings(_SQLITE_URL, timeout_ms=120000) == {}


def test_server_settings_reads_settings_knob_when_no_override(monkeypatch) -> None:
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "idle_in_transaction_session_timeout_ms", 5000)
    assert idle_tx_server_settings(_PG_URL) == {"idle_in_transaction_session_timeout": "5000"}


# --------------------------------------------------------------------------
# Unit — create_app_engine forwards the server_setting into connect_args
# --------------------------------------------------------------------------
def test_create_app_engine_injects_connect_args_when_knob_positive(monkeypatch) -> None:
    captured: dict = {}

    def _fake_create(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(engine_mod, "create_async_engine", _fake_create)
    create_app_engine(_PG_URL, timeout_ms=120000)

    server_settings = captured["kwargs"]["connect_args"]["server_settings"]
    assert server_settings["idle_in_transaction_session_timeout"] == "120000"


def test_create_app_engine_omits_connect_args_when_knob_zero(monkeypatch) -> None:
    captured: dict = {}

    def _fake_create(url, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(engine_mod, "create_async_engine", _fake_create)
    create_app_engine(_PG_URL, timeout_ms=0)

    assert "connect_args" not in captured["kwargs"]


# --------------------------------------------------------------------------
# Real Postgres — the guard actually terminates a leaked idle transaction
# --------------------------------------------------------------------------
async def test_idle_in_transaction_timeout_kills_leaked_session() -> None:
    """Prove the GUC is really applied to app connections.

    Open a session, run ``SELECT 1`` (asyncpg autobegins a transaction — the
    connection is now ``idle in transaction``), sleep PAST a SHORT timeout
    WITHOUT committing, then assert the next use of that connection raises:
    Postgres has terminated the session with
    ``idle_in_transaction_session_timeout``. This is exactly the leak the prod
    incident suffered — here it self-heals instead of wedging the pool.
    """
    if not use_real_pg():
        pytest.skip("real Postgres required — idle_in_transaction_session_timeout is a PG-only GUC")

    engine = create_app_engine(pg_url(), timeout_ms=1000)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            # autobegins a transaction → connection is now idle-in-transaction
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            # leak it: sleep well past the 1000ms guard WITHOUT committing
            await asyncio.sleep(2.0)
            with pytest.raises(sqlalchemy.exc.DBAPIError) as exc_info:
                await session.execute(text("SELECT 1"))
            # PG's termination reason surfaces through the wrapped asyncpg error.
            assert (
                "idle" in str(exc_info.value).lower()
                or "terminat" in str(exc_info.value).lower()
                or "connection" in str(exc_info.value).lower()
            )
    finally:
        await engine.dispose()
