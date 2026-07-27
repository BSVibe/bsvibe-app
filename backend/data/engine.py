"""Async SQLAlchemy engine factory — the single source for app DB connections.

Every APP DB engine (API request sessions in ``backend.api.deps``, the worker
runtime in ``backend.workflow...runtime.lifecycle``, and the ad-hoc
``backend.data.session.session_scope`` helper) is built through
:func:`create_app_engine` so one connection policy governs them all — DRY, and
legible in the class diagram (one factory, one place the policy lives).

WHY the idle-in-transaction guard
----------------------------------
A prod incident: leaked / held-open DB transactions (sessions left ``idle in
transaction``) accumulated (~15) and exhausted the SQLAlchemy connection pool.
``/api/v1/workers/heartbeat`` and every other DB endpoint then hung → a full
outage that needed a manual backend restart to clear.

A just-merged refactor (#632) made the drive loop use SHORT transactions (no
pooled connection is held across the multi-minute executor turn), so **no
legitimate app operation now holds a transaction idle for more than a few
seconds.** That makes it SAFE to have Postgres auto-kill any session left idle
in a transaction past ``idle_in_transaction_session_timeout``: the timeout only
ever catches a genuine LEAK, and the pool self-heals instead of requiring a
manual restart.

The value is applied via asyncpg ``connect_args={"server_settings": {...}}`` at
engine creation — code-controlled, version-controlled, and deploys with the
app. It is tunable through ``settings.idle_in_transaction_session_timeout_ms``
(``0`` disables it). We deliberately do NOT set ``statement_timeout`` — that
would kill legitimate long-running ACTIVE queries; only the idle-in-transaction
guard is in scope.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.config import get_settings


def _is_asyncpg_url(url: str) -> bool:
    """True when ``url`` targets Postgres via asyncpg.

    The ``server_settings`` connect-arg is asyncpg-specific; injecting it into a
    SQLite/aiosqlite engine (used throughout the unit suite) would break the
    connection. Guarding by driver keeps the factory usable for every URL.
    """
    try:
        return "asyncpg" in make_url(url).drivername
    except Exception:  # noqa: BLE001 — a malformed URL simply isn't asyncpg
        return "asyncpg" in url


def idle_tx_server_settings(url: str, timeout_ms: int | None = None) -> dict[str, str]:
    """The asyncpg ``server_settings`` carrying the idle-in-transaction guard.

    Returns ``{"idle_in_transaction_session_timeout": "<ms>"}`` when the guard
    is active, else an EMPTY dict — which happens when the timeout is ``<= 0``
    (disabled) or the target is not an asyncpg URL (e.g. SQLite in tests).
    asyncpg wants the GUC value as a string of milliseconds.

    ``timeout_ms`` defaults to ``settings.idle_in_transaction_session_timeout_ms``.
    """
    ms = get_settings().idle_in_transaction_session_timeout_ms if timeout_ms is None else timeout_ms
    if ms > 0 and _is_asyncpg_url(url):
        return {"idle_in_transaction_session_timeout": str(ms)}
    return {}


def create_app_engine(
    url: str | None = None,
    *,
    timeout_ms: int | None = None,
    **kwargs: Any,
) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for an app DB connection.

    Injects the idle-in-transaction guard (see module docstring) into asyncpg
    ``connect_args`` — merging with any caller-supplied ``server_settings`` —
    and defaults ``future=True``. Non-asyncpg URLs and a ``0`` timeout leave the
    connection untouched. ``url`` defaults to ``settings.database_url``.
    """
    resolved = url or get_settings().database_url

    server_settings = idle_tx_server_settings(resolved, timeout_ms)
    if server_settings:
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args["server_settings"] = {
            **connect_args.get("server_settings", {}),
            **server_settings,
        }
        kwargs["connect_args"] = connect_args

    kwargs.setdefault("future", True)
    return create_async_engine(resolved, **kwargs)


__all__ = ["create_app_engine", "idle_tx_server_settings"]
