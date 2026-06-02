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
from backend.workflow.application.runtime.agent_runtime import build_agent_execution_deps
from backend.workflow.application.runtime.delivery_runtime import (
    build_delivery_adapter,
    load_connector_plugins,
)
from backend.workflow.application.runtime.worker_runtime import (
    build_worker_runtime,
    check_executor_dispatch_health,
    run_stream_consumers,
)
from plugin.audit import register_audit_subscriber

logger = structlog.get_logger(__name__)


async def run_workers() -> None:
    """Process entrypoint — construct + run every worker until SIGINT/SIGTERM.

    Wired by ``python -m backend.workers`` (see ``backend/workers/__main__.py``).
    Default ``worker_mode="db_polling"`` runs the poll-loop runtime exactly as
    before; ``worker_mode="redis_streams"`` runs the Redis-consumer runtime.
    """
    settings = get_settings()
    register_audit_subscriber()
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # The Redis client is needed by (a) redis_streams mode's producer-side
    # wake-up emission and (b) executor-pool dispatch (Lift 5b) — the
    # ExecutorOrchestrator XADDs a task onto the worker's stream + awaits the
    # done channel, even in the default db_polling mode. So it is built
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
            logger.info("worker_live_event_bus_redis_bound", redis_url=settings.redis_url)

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
    runtime = build_worker_runtime(
        session_factory=session_factory,
        execution=execution,
        delivery_adapter=delivery_adapter,
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
