"""Action Protocol + standalone ``@action`` decorator.

Two surfaces:

1. :class:`Action` — runtime_checkable Protocol describing the call shape
   of a registered action capability. Mirrors the Lift G engine Protocol
   at ``backend.extensions.domain.protocols.Action``.

2. :func:`action` — a *standalone* decorator marker for plugin authors
   who prefer free-function declaration over the builder-style
   ``@p.action(...)``. The decorator attaches metadata only; the
   engine's plugin loader (Lift S keeps it at
   ``backend.extensions.plugin.decorator``) is what materializes
   :class:`backend.extensions.plugin.base.ActionCapability` records.

Per v8 §D42 the SDK stays plugin-only and engine-free. The richer
``PluginBuilder.action`` form remains the *recommended* author API
(see :mod:`bsvibe_sdk.plugin`); the standalone form here is a thin
fallback for tooling that wants to inspect action metadata without
constructing a builder.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

# Attribute names the engine introspects to discover decorated functions.
_ACTION_NAME_ATTR = "__bsvibe_action_name__"
_ACTION_MCP_ATTR = "__bsvibe_action_mcp_exposed__"
_ACTION_SCHEMA_ATTR = "__bsvibe_action_input_schema__"


@runtime_checkable
class Action(Protocol):
    """A registered plugin action.

    The concrete carrier inside the engine is
    :class:`backend.extensions.plugin.base.ActionCapability`; this Protocol
    is the SDK-facing call shape.
    """

    name: str

    async def __call__(self, context: Any, /, **kwargs: Any) -> Any: ...


def action(
    *,
    name: str,
    mcp_exposed: bool = False,
    input_schema: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Mark a free async function as a plugin action.

    Usage::

        from bsvibe_sdk import Context, action

        @action(name="open_pr", mcp_exposed=True)
        async def open_pr(context: Context, *, branch: str, title: str) -> dict:
            ...

    The decorator attaches metadata; binding the function to a plugin
    instance is the loader's job. Compatible with ``@p.action(...)`` —
    plugins may mix both styles.
    """
    if not name:
        raise ValueError("action: name must be non-empty")

    def decorator(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        setattr(fn, _ACTION_NAME_ATTR, name)
        setattr(fn, _ACTION_MCP_ATTR, mcp_exposed)
        setattr(fn, _ACTION_SCHEMA_ATTR, input_schema)
        return fn

    return decorator


__all__ = ["Action", "action"]
