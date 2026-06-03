"""MCP tool registrations — one entry point for the entire D2 surface."""

from __future__ import annotations

from backend.mcp.api import ToolRegistry
from backend.mcp.tools.direct_tools import register_direct_tools
from backend.mcp.tools.knowledge_tools import register_knowledge_tools
from backend.mcp.tools.safe_mode_tools import register_safe_mode_tools
from backend.mcp.tools.workflow_tools import register_workflow_tools


def register_all_tools(registry: ToolRegistry) -> None:
    """Register every D2 MCP tool onto ``registry``.

    Surfaces: knowledge (5), workflow (6), safe-mode (3), direct (1).
    """
    register_knowledge_tools(registry)
    register_workflow_tools(registry)
    register_safe_mode_tools(registry)
    register_direct_tools(registry)


__all__ = [
    "register_all_tools",
    "register_direct_tools",
    "register_knowledge_tools",
    "register_safe_mode_tools",
    "register_workflow_tools",
]
