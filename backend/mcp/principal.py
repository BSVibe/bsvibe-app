"""Per-request principal context-var for the MCP Streamable HTTP transport.

The MCP SDK's ``CallTool`` path does NOT thread HTTP headers down to the
tool handler — by the time the dispatcher runs, the original request is
out of scope. The Streamable HTTP transport
(:mod:`backend.mcp.streamable_http`) therefore resolves the principal
from the ``Authorization`` header up-front and stashes it on the
contextvar declared here; the dispatcher reads it back to build the
:class:`backend.mcp.api.ToolContext`.

``None`` (the default outside an active request, or when the request
carried no/invalid bearer token) makes every permissioned tool deny — by
that point the transport has already returned a 401 to the wire, so a
``None`` read here is a test-mode / belt-and-braces guard.
"""

from __future__ import annotations

from contextvars import ContextVar

from backend.mcp.api import McpPrincipal

_principal_var: ContextVar[McpPrincipal | None] = ContextVar(
    "_bsvibe_mcp_principal",
    default=None,
)


def get_request_principal() -> McpPrincipal | None:
    """Return the principal resolved for the current MCP request, if any."""
    return _principal_var.get()


def set_request_principal(principal: McpPrincipal | None) -> object:
    """Bind ``principal`` to the active request scope; returns a reset token."""
    return _principal_var.set(principal)


def reset_request_principal(token: object) -> None:
    """Reset the principal to its prior value using the token from ``set_*``."""
    _principal_var.reset(token)  # type: ignore[arg-type]


__all__ = [
    "get_request_principal",
    "reset_request_principal",
    "set_request_principal",
]
