"""Base ASGI middleware shared by every BSVibe FastAPI service.

:class:`RequestIdMiddleware` assigns each request a stable correlation
id, exposes it on ``request.state.request_id``, echoes it on the
``x-request-id`` response header, and binds it to the structlog
``contextvars`` so every log line emitted inside the request handler
carries ``request_id=<value>`` automatically.

Why structlog ``contextvars`` instead of a plain context-manager:

* Async handlers can ``await`` deep into other modules — only
  structlog's ``contextvars`` integration survives that boundary.
* Each request runs in its own asyncio task, which gets a fresh copy of
  the contextvars context, so cross-request leakage is impossible.
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "x-request-id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a request id and bind it to structlog contextvars.

    The header name is case-insensitive (HTTP semantics). An empty
    incoming value is treated as missing — a fresh UUID4 is generated so
    log scrapers always see a non-empty ``request_id``.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next,
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming.strip() or uuid.uuid4().hex
        request.state.request_id = request_id

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = ["RequestIdMiddleware", "REQUEST_ID_HEADER"]
