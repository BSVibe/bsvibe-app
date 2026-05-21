"""Bind :func:`backend.skills.invoke_skill` into the execution ToolRegistry.

The seam Bundle S ↔ Bundle X. Skill loaders are workspace-scoped (FS-based
under ``skills/<workspace_id>/``); the execution layer's ToolRegistry is
per-RunAttempt. This module is the adapter that registers a single
``invoke_skill`` ToolDefinition pointing at a particular loader + completion_fn.

Workflow §6 #5 — every workspace's agent loop gets one ``invoke_skill`` tool
in its registered tools set; calling it with ``{"name": "...", "input": "..."}``
runs the named skill end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from backend.execution.tools import ToolDefinition, ToolRegistry
from backend.skills.loader import SkillLoader
from backend.skills.runner import CompletionFn, Searcher, invoke_skill

INVOKE_SKILL_NAME = "invoke_skill"

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Skill name (must match a manifest in this workspace's skills/).",
        },
        "input": {
            "type": "string",
            "description": "User-facing input the skill should process.",
        },
    },
    "required": ["name", "input"],
}


def register_invoke_skill(
    registry: ToolRegistry,
    *,
    loader: SkillLoader,
    completion_fn: CompletionFn,
    searcher: Searcher | None = None,
) -> None:
    """Add an ``invoke_skill`` tool bound to ``loader`` + ``completion_fn``.

    Idempotent for repeat calls on the same registry — re-registration
    raises ``ToolError`` per the registry contract; if you need to
    re-bind, construct a fresh ToolRegistry.
    """
    available_tools = registry.names()

    async def handler(arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        user_input = arguments.get("input", "")
        if not name:
            return json.dumps({"error": "name is required"})
        try:
            result = await invoke_skill(
                name=name,
                user_input=user_input,
                loader=loader,
                completion_fn=completion_fn,
                searcher=searcher,
                available_tools=available_tools,
            )
        except Exception as exc:  # noqa: BLE001 — surface to LLM, not crash
            return json.dumps({"error": str(exc), "skill": name})
        return json.dumps(
            {
                "skill": result.skill_name,
                "response": result.response,
                "used_tools": result.used_tools,
                "retrieval_chars": result.retrieval_chars,
            }
        )

    description = (
        "Invoke a workspace-installed skill by name. Returns a JSON object "
        "with the skill's response and metadata."
    )
    registry.register(
        ToolDefinition(
            name=INVOKE_SKILL_NAME,
            description=description,
            parameters_schema=_PARAMS_SCHEMA,
            handler=handler,
        )
    )


# Optional alias used by callers building tool sets via ``Callable[[ToolRegistry], None]``.
ToolBinder = Callable[[ToolRegistry], Awaitable[None] | None]


__all__ = ["INVOKE_SKILL_NAME", "ToolBinder", "register_invoke_skill"]
