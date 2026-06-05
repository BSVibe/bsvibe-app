"""MCP tool registrations — one entry point for the entire D2 surface."""

from __future__ import annotations

from backend.mcp.api import ToolRegistry
from backend.mcp.tools.connectors_tools import register_connectors_tools
from backend.mcp.tools.direct_tools import register_direct_tools
from backend.mcp.tools.knowledge_tools import register_knowledge_tools
from backend.mcp.tools.model_accounts_tools import register_model_accounts_tools
from backend.mcp.tools.notifications_tools import register_notifications_tools
from backend.mcp.tools.safe_mode_tools import register_safe_mode_tools
from backend.mcp.tools.workflow_tools import register_workflow_tools


def register_all_tools(registry: ToolRegistry) -> None:
    """Register every MCP tool onto ``registry``.

    Surfaces (D2 + D3a):
    knowledge (5), workflow (7), safe-mode (3), direct (1),
    model-accounts (4 — D3a), connectors (5 — D3a),
    notifications (2 — D3a).
    """
    register_knowledge_tools(registry)
    register_workflow_tools(registry)
    register_safe_mode_tools(registry)
    register_direct_tools(registry)
    register_model_accounts_tools(registry)
    register_connectors_tools(registry)
    register_notifications_tools(registry)


__all__ = [
    "register_all_tools",
    "register_connectors_tools",
    "register_direct_tools",
    "register_knowledge_tools",
    "register_model_accounts_tools",
    "register_notifications_tools",
    "register_safe_mode_tools",
    "register_workflow_tools",
]
