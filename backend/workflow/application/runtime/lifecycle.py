"""Worker process lifecycle / boot path (§17.2a slice).

Wires settings → engine → session factory → Redis (optional) → plugin
registry → execution deps → delivery adapter → :class:`WorkerRuntime` →
signal handlers → graceful drain.

This is the thin lifecycle layer that ``python -m backend.workers`` boots
into (via ``backend/workers/__main__.py``). Construction lives in the
adjacent runtime/ modules; only the boot orchestration is here.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import redis.asyncio as redis_aio
import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.shared.core.http import redact_url_password
from backend.workflow.application.runtime.agent_runtime import build_agent_execution_deps
from backend.workflow.application.runtime.delivery_runtime import (
    build_delivery_adapter,
    load_connector_plugins,
)
from backend.workflow.application.runtime.notify_runtime import build_notify_sender
from backend.workflow.application.runtime.worker_runtime import (
    build_worker_runtime,
    check_executor_dispatch_health,
    run_stream_consumers,
)
from plugin.audit import register_audit_subscriber

logger = structlog.get_logger(__name__)


async def _bootstrap_db_oauth_providers(session_factory: Any) -> None:
    """Register connector OAuth providers from DB-stored App credentials (the
    GitHub App Manifest flow) in the WORKER process — issue #362.

    Delivery + connector token refresh run in the worker, but
    ``load_app_credential_providers`` was only called in the API lifespan. So
    ``get_provider("github")`` was ``None`` here and
    ``resolve_connector_credentials`` silently skipped refresh — an expired
    github push token then failed the ``deliver_github`` push and no PR opened.
    Mirrors ``backend/api/main.py``; soft-fail so a DB hiccup / pre-migration DB
    never blocks worker boot (the connector just falls back to env / legacy).
    """
    from backend.connectors.auth.bootstrap import (  # noqa: PLC0415 — lazy
        load_app_credential_providers,
    )
    from backend.router.accounts.crypto import (  # noqa: PLC0415 — lazy
        CredentialCipher,
        _key_from_settings,
    )

    try:
        async with session_factory() as session:
            await load_app_credential_providers(session, CredentialCipher(_key_from_settings()))
    except Exception:  # noqa: BLE001 — provider load must never break worker boot
        logger.warning("connector_oauth_db_provider_load_failed", exc_info=True)


async def run_workers() -> None:
    """Process entrypoint — construct + run every worker until SIGINT/SIGTERM.

    Wired by ``python -m backend.workers`` (see ``backend/workers/__main__.py``).
    Default ``worker_mode="db_polling"`` runs the poll-loop runtime exactly as
    before; ``worker_mode="redis_streams"`` runs the Redis-consumer runtime.
    """
    # T2b-1 — this process MINTS the run-scoped task token that the backend's MCP API
    # verifies. With a per-process ephemeral signing key the two can never agree (measured:
    # 401 invalid_token), and every restart silently invalidates every outstanding token.
    # Refuse to boot a deployment without a shared PEM rather than mint tokens nobody can
    # verify.
    from backend.identity.oauth_keys import (  # noqa: PLC0415
        ensure_signing_key_is_shareable,
    )

    _settings = get_settings()
    ensure_signing_key_is_shareable(
        pem=_settings.oauth_private_key_pem, environment=_settings.environment
    )

    settings = get_settings()
    register_audit_subscriber()
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # #362 — register DB-stored connector OAuth providers so the worker can
    # REFRESH expiring connector tokens during delivery (github push, etc.).
    await _bootstrap_db_oauth_providers(session_factory)

    # The Redis client is needed by (a) redis_streams mode's producer-side
    # wake-up emission and (b) executor-pool dispatch — the
    # :class:`~backend.dispatch.adapter.ExecutorAdapter` XADDs a chat task onto
    # the worker's stream + awaits the done channel, even in the default
    # db_polling mode. So it is built
    # whenever a Redis URL is configured (the default is set), and threaded
    # through the orchestrator factory. ``decode_responses=True`` matches the
    # dispatch substrate's flat-string contract.
    redis_client: Any = None
    if settings.redis_url:
        redis_client = redis_aio.from_url(settings.redis_url, decode_responses=True)
        # C2 — bind the worker process's LiveEventBus singleton against the
        # SAME Redis transport the backend HTTP container binds against, so
        # audit-emit publishes from the worker land on the channel the SSE
        # subscribers in the backend container are subscribed to.
        #
        # Skip under pytest for the same reason as create_app() — a real
        # redis client held in the process-wide singleton leaks
        # connection-pool Futures across per-test event loops.
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            # Lazy import to avoid circular module-init: backend.api.v1
            # re-exports safemode which imports backend.workflow.infrastructure.workers.run.
            from backend.api.v1.live_events import (  # noqa: PLC0415
                set_live_event_bus_redis,
            )

            set_live_event_bus_redis(redis_client)
            logger.info(
                "worker_live_event_bus_redis_bound",
                redis_url=redact_url_password(settings.redis_url),
            )

    # B14 — operator visibility: warn LOUDLY at startup when the executor pool
    # is configured but Redis is not.
    await check_executor_dispatch_health(
        session_factory=session_factory, redis_url=settings.redis_url
    )

    # B5b — load the plugin registry ONCE so each per-run native loop can
    # surface the workspace's connector actions as tools. Shared across runs;
    # the per-run resolver only adds the run's session. Lift 0c removed the
    # load-time danger verdict map.
    connector_plugins = await load_connector_plugins(settings=settings)
    execution = build_agent_execution_deps(
        settings=settings,
        redis_client=redis_client,
        connector_plugins=connector_plugins,
    )
    delivery_adapter = await build_delivery_adapter(session_factory=session_factory)
    # Notifier N2 — the founder-notification push sender reuses the SAME loaded
    # plugin registry + settings cipher as delivery (no second plugin load, no
    # second key source).
    from backend.router.accounts.crypto import (  # noqa: PLC0415 — lazy, matches oauth bootstrap
        CredentialCipher,
        _key_from_settings,
    )

    notify_sender = build_notify_sender(
        plugins=list(connector_plugins.values()),
        cipher=CredentialCipher(_key_from_settings()),
    )
    runtime = build_worker_runtime(
        session_factory=session_factory,
        execution=execution,
        delivery_adapter=delivery_adapter,
        notify_sender=notify_sender,
        settings=settings,
        redis_client=redis_client,
    )

    if settings.worker_mode == "redis_streams":
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # pragma: no cover — non-POSIX
                pass
        try:
            await run_stream_consumers(
                workers=runtime.workers,
                redis_client=redis_client,
                stop_event=stop_event,
            )
        finally:
            await redis_client.aclose()
            await engine.dispose()
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.request_stop)
        except NotImplementedError:  # pragma: no cover — non-POSIX
            pass

    try:
        await runtime.run_forever()
    finally:
        if redis_client is not None:
            await redis_client.aclose()
        await engine.dispose()


__all__ = ["run_workers"]
