"""Embedded MCP server — Lift D2.

bsvibe-app fronts an MCP server at ``/mcp`` so Claude Code (and any
RFC-9728-compliant MCP client) can drive runs, Safe Mode, products,
knowledge, and the Direct trigger over the wire. Authentication is the
ES256 access token issued by the embedded OAuth server (Lift D1); the
Streamable HTTP transport verifies the Bearer token + scopes per
request and stashes the resolved principal on a context-var the
dispatcher reads to build :class:`ToolContext`.

The two halves of the package:

* :mod:`backend.mcp.api` / :mod:`backend.mcp.server` — transport-agnostic
  registry, dispatcher, MCP server factory.
* :mod:`backend.mcp.auth` / :mod:`backend.mcp.principal` /
  :mod:`backend.mcp.streamable_http` — auth-and-transport seam.

Tools live under :mod:`backend.mcp.tools`.
"""

from __future__ import annotations

from backend.mcp.api import (
    McpPrincipal,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolScopeDenied,
)
from backend.mcp.server import build_registry, build_server

__all__ = [
    "McpPrincipal",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolScopeDenied",
    "build_registry",
    "build_server",
]
