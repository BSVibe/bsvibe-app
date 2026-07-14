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

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.mcp.server import build_registry, build_server
from backend.mcp.streamable_http import build_streamable_http_app

logger = structlog.get_logger(__name__)


@contextlib.asynccontextmanager
async def mcp_lifespan(
    app: FastAPI,
    *,
    session_factory: async_sessionmaker[Any],
    delivery_dispatcher: Any | None = None,
    record_question: Any | None = None,
    record_deliverable: Any | None = None,
) -> AsyncIterator[None]:
    """Bring up the MCP transport for the duration of the FastAPI app.

    Lift E40 — ``delivery_dispatcher`` (optional): the outbound
    :class:`PluginDispatchAdapter` injected from the FastAPI app so the
    MCP ``bsvibe_safe_mode_approve`` handler dispatches through the
    same code path as the REST route. Passed through to
    :func:`build_server` and installed into every :class:`ToolContext`.
    """
    settings = get_settings()
    issuer = settings.oauth_issuer
    registry = build_registry(
        record_question=record_question, record_deliverable=record_deliverable
    )
    server = build_server(
        session_factory=session_factory,
        registry=registry,
        delivery_dispatcher=delivery_dispatcher,
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
    async with manager.run():
        try:
            yield
        finally:
            logger.info("mcp_lifespan_stopping")


__all__ = ["mcp_lifespan"]
