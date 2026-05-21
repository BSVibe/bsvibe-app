"""Plugin → MCP tool adapter.

Plugins opt in via ``mcp_exposed=True`` on the ``@plugin`` decorator. This
module reads the plugin registry and produces MCP-spec tool descriptors,
then routes ``tools/call`` invocations through the existing AgentLoop so
the same execution path serves REST, MCP stdio, and MCP SSE.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


_DEFAULT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


async def list_plugins_as_tools(state: Any) -> list[dict[str, Any]]:
    """Return MCP tool descriptors for all plugins with ``mcp_exposed=True``.

    Disabled plugins (``runtime_config.disabled_entries``) are filtered out.
    Each descriptor has ``name``, ``description``, ``inputSchema`` per MCP spec.
    """
    registry = await state.plugin_loader.load_all()
    disabled = set(getattr(state.runtime_config, "disabled_entries", []) or [])

    tools: list[dict[str, Any]] = []
    for meta in registry.values():
        if not getattr(meta, "mcp_exposed", False):
            continue
        if meta.name in disabled:
            continue
        schema = _normalize_schema(meta.input_schema)
        tools.append(
            {
                "name": meta.name,
                "description": meta.description or f"Plugin: {meta.name}",
                "inputSchema": schema,
            }
        )
    return tools


async def invoke_plugin_as_tool(
    state: Any,
    plugin_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run an mcp_exposed plugin via AgentLoop.on_input.

    Raises:
        KeyError: plugin not registered.
        PermissionError: plugin not mcp_exposed or currently disabled.
    """
    meta = state.agent_loop.get_entry(plugin_name)  # raises KeyError
    if not getattr(meta, "mcp_exposed", False):
        raise PermissionError(f"Plugin '{plugin_name}' is not exposed to MCP")

    disabled = set(getattr(state.runtime_config, "disabled_entries", []) or [])
    if plugin_name in disabled:
        raise PermissionError(f"Plugin '{plugin_name}' is disabled")

    try:
        results = await state.agent_loop.on_input(plugin_name, params)
        return {
            "plugin": plugin_name,
            "success": True,
            "results": results,
        }
    except Exception as exc:
        logger.exception("mcp_plugin_invoke_failed", plugin=plugin_name)
        return {
            "plugin": plugin_name,
            "success": False,
            "error": str(exc),
        }


def _normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a plugin's input_schema into an MCP-compliant JSON Schema object.

    The MCP spec requires ``inputSchema`` to be an object-typed JSON Schema.
    Plugins that omit a schema get an open object that accepts arbitrary fields.
    """
    if not schema:
        return dict(_DEFAULT_INPUT_SCHEMA)
    if schema.get("type") != "object":
        return {**_DEFAULT_INPUT_SCHEMA, "properties": schema}
    return schema
