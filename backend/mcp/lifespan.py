"""FastAPI lifespan that mounts the MCP Streamable HTTP transport — Lift D2.

The MCP SDK's :class:`StreamableHTTPSessionManager` creates an internal
``anyio`` task group inside its ``run()`` context manager. Per-request
handlers raise ``RuntimeError`` if invoked before that task group is up.
The lifespan declared here:

1. constructs the registry + MCP server + manager
2. enters ``manager.run()`` for the lifetime of the app
3. exposes the authenticated ASGI app on ``app.state.mcp_asgi`` so
   :mod:`backend.api.main` can mount it at ``/mcp``
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.mcp.home_forwarding import build_home_routing, serve_home_calls
from backend.mcp.server import build_registry, build_server, build_tool_executor
from backend.mcp.streamable_http import build_streamable_http_app
from backend.workers.emit import get_dispatch_redis_client
from backend.workflow.infrastructure.sandbox.reaper import sandbox_reaper_loop
from backend.workflow.infrastructure.sandbox.resolver import get_sandbox_manager

logger = structlog.get_logger(__name__)


@contextlib.asynccontextmanager
async def mcp_lifespan(
    app: FastAPI,
    *,
    session_factory: async_sessionmaker[Any],
    delivery_dispatcher: Any | None = None,
    client_sandbox: Any | None = None,
    declared_secrets: Any | None = None,
    retract_handler: Any | None = None,
    narrative_generator: Any | None = None,
    record_question: Any | None = None,
    record_deliverable: Any | None = None,
    record_progress: Any | None = None,
) -> AsyncIterator[None]:
    """Bring up the MCP transport for the duration of the FastAPI app.

    Lift E40 — ``delivery_dispatcher`` (optional): the outbound
    :class:`PluginDispatchAdapter` injected from the FastAPI app so the
    MCP ``bsvibe_safe_mode_approve`` handler dispatches through the
    same code path as the REST route. Passed through to
    :func:`build_server` and installed into every :class:`ToolContext`.

    ``retract_handler`` (optional) — same shape again: the plugin-compensation
    runtime behind ``bsvibe_deliverables_retract``. The retraction *rule* is in
    ``workflow.application``; only the runtime that calls a plugin is handed in.

    ``client_sandbox`` (optional) — same shape: resolves the founder's own machine for a
    ``client_attach`` run, injected because addressing it reaches the executor dispatch
    substrate, which the MCP import contract forbids this context.

    ``declared_secrets`` (optional) — same shape: a run's product's declared secrets for its
    server box (#691); unsealing them reaches the credential cipher.
    """
    settings = get_settings()
    issuer = settings.oauth_issuer
    registry = build_registry(
        record_question=record_question,
        record_deliverable=record_deliverable,
        record_progress=record_progress,
    )
    # #929 (B) — with more than one machine, a run's work-tool calls are answered by the
    # machine holding its worktree. None on a single box: everything runs in place.
    home_routing = build_home_routing(settings, get_dispatch_redis_client(settings))
    effects: dict[str, Any] = {
        "delivery_dispatcher": delivery_dispatcher,
        "client_sandbox": client_sandbox,
        "declared_secrets": declared_secrets,
        "retract_handler": retract_handler,
        "narrative_generator": narrative_generator,
    }
    server = build_server(
        session_factory=session_factory,
        registry=registry,
        home_routing=home_routing,
        **effects,
    )
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
    )
    asgi_app = build_streamable_http_app(
        issuer=issuer,
        session_factory=session_factory,
        manager=manager,
    )
    app.state.mcp_registry = registry
    app.state.mcp_asgi = asgi_app
    logger.info("mcp_lifespan_starting", tools=registry.names(), issuer=issuer)
    # #929 — release the sandboxes this process made for runs' tool calls once they sit idle.
    # Nothing else ever does once a run has moved to another machine.
    sandbox_manager = get_sandbox_manager()
    reaping = (
        asyncio.create_task(sandbox_reaper_loop(sandbox_manager))
        if sandbox_manager is not None
        else None
    )
    stop_serving = asyncio.Event()
    serving: asyncio.Task[None] | None = None
    if home_routing is not None:
        # This machine answers the calls other machines forward to it. They run here like
        # any other call, except that they never bounce on (``forwarded=True``).
        execute = build_tool_executor(
            session_factory=session_factory,
            registry=registry,
            home_routing=home_routing,
            **effects,
        )
        serving = asyncio.create_task(
            serve_home_calls(
                home_routing.redis,
                machine=home_routing.machine,
                execute=functools.partial(execute, forwarded=True),
                stop=stop_serving,
            )
        )
    async with manager.run():
        try:
            yield
        finally:
            logger.info("mcp_lifespan_stopping")
            stop_serving.set()
            if reaping is not None:
                reaping.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reaping
            if serving is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(serving, timeout=5)


__all__ = ["mcp_lifespan"]
