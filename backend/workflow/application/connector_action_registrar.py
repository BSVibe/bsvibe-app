"""Connector action registrar — bind workspace connector actions to the loop.

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1).
When the agent loop is given a :class:`ConnectorActionProvider`, this
module surfaces the workspace's available ``mcp_exposed`` connector
actions (github open_pr, notion create_page, …) as loop tools. Each
tool's handler is bound to THIS run; the handler resolves the
account credentials and dispatches the action through the provider,
feeding the result back to the work LLM as a JSON string.

Lift 0c removed the load-time + per-call ``DangerAnalyzer`` gating that
used to wrap each handler — per-call gating can be re-introduced from a
real producer when there is a concrete need.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from backend.workflow.infrastructure.connector_actions import (
    ConnectorActionProvider,
    ConnectorActionTool,
    loop_tool_name,
)
from backend.workflow.infrastructure.db import ExecutionRun, WorkStep
from backend.workflow.infrastructure.tools import ToolDefinition, ToolRegistry

logger = structlog.get_logger(__name__)


def _connector_action_schema(tool: ConnectorActionTool) -> dict[str, Any]:
    """The OpenAI-style parameters schema for a connector action tool.

    Reuses the action's declared ``input_schema`` (validated by the runner on
    dispatch) when present; otherwise an open object so the LLM can still pass
    arguments through to a schema-less action."""
    schema = tool.action.input_schema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _connector_action_description(tool: ConnectorActionTool) -> str:
    return (
        f"Take the '{tool.action_name}' action on the '{tool.connector}' connector "
        "for this workspace. The connector credentials are injected automatically — "
        "supply only the action arguments."
    )


def _make_connector_action_handler(
    provider: ConnectorActionProvider,
    tool: ConnectorActionTool,
    *,
    run: ExecutionRun,
) -> Any:
    """Build the registry handler for one connector action.

    The handler resolves + decrypts the account credentials into the action
    context and dispatches the action, feeding the result back to the loop.
    Never raises into the loop (failures become a readable tool result).
    """

    async def handler(arguments: dict[str, Any]) -> str:
        try:
            credentials = provider.credentials_for(tool)
            result = await provider.dispatch(tool, credentials=credentials, kwargs=arguments)
        except Exception as exc:  # noqa: BLE001 — surface to LLM, never crash the loop
            logger.warning(
                "connector_action_dispatch_failed",
                run_id=str(run.id),
                connector=tool.connector,
                action=tool.action_name,
                error=str(exc),
            )
            return json.dumps(
                {
                    "status": "error",
                    "connector": tool.connector,
                    "action": tool.action_name,
                    "error": str(exc),
                }
            )
        logger.info(
            "connector_action_dispatched",
            run_id=str(run.id),
            connector=tool.connector,
            action=tool.action_name,
        )
        return json.dumps(
            {
                "status": "ok",
                "connector": tool.connector,
                "action": tool.action_name,
                "result": result,
            },
            default=str,
        )

    return handler


async def register_connector_action_tools(
    registry: ToolRegistry,
    *,
    provider: ConnectorActionProvider | None,
    run: ExecutionRun,
    work_step: WorkStep,
) -> list[str]:
    """Register the workspace's available connector actions into ``registry``.

    Only when the orchestrator was given a :class:`ConnectorActionProvider`
    (the production worker factory threads one in). Each tool's handler is
    bound to THIS run + work_step. Returns the surfaced tool names
    (namespaced ``<connector>__<action>``). No provider, or a workspace with
    no connector accounts → empty list (loop unchanged).

    Lift 0c removed the load-time + per-call danger gating that used to
    wrap each handler. Per-call gating can be re-introduced from a real
    producer (a manual ``@p.action(dangerous=True)`` opt-in) when there is
    a concrete need.
    """
    if provider is None:
        return []
    tools = await provider.list_actions(run.workspace_id)
    if not tools:
        return []
    names: list[str] = []
    for tool in tools:
        name = loop_tool_name(tool.connector, tool.action_name)
        registry.register(
            ToolDefinition(
                name=name,
                description=_connector_action_description(tool),
                parameters_schema=_connector_action_schema(tool),
                handler=_make_connector_action_handler(provider, tool, run=run),
            )
        )
        names.append(name)
    logger.info(
        "connector_action_tools_registered",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        tools=names,
    )
    return names


__all__ = [
    "_connector_action_description",
    "_connector_action_schema",
    "_make_connector_action_handler",
    "register_connector_action_tools",
]
