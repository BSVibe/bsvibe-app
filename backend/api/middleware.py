"""HTTP middleware — workspace request-context lifecycle (Workflow §3, layer 1).

The :data:`backend.data.scoping.current_workspace_id` contextvar is populated
by the workspace-resolution dependency during request handling. This pure-ASGI
middleware brackets each request so the contextvar always starts unset and is
reset afterwards — guaranteeing one request can never leak its workspace scope
into the next.

Pure ASGI (not ``BaseHTTPMiddleware``) on purpose: it runs in the *same* task
as the endpoint, so the value the dependency sets downstream is visible to the
ORM auto-filter and the reset here actually unwinds it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from backend.data.scoping import current_workspace_id


class WorkspaceContextMiddleware:
    """Reset the workspace contextvar at the boundary of every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = current_workspace_id.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_workspace_id.reset(token)
