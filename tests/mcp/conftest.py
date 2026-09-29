"""Shared fixtures for the MCP tool tests.

Most modules here call ``registry.call_tool(...)`` DIRECTLY, skipping
``backend/mcp/server.py``'s ``_call_tool`` — and with it the step that
publishes the principal's workspace (layer 2 contextvar + layer 3 Postgres
GUC) before the tool runs. Under a fail-OPEN RLS policy that gap was invisible;
under fail-CLOSED (#959 ③) the tool runs blind: its writes are refused and its
reads come back empty.

:func:`dispatch_publishes_workspace` restores that step for a module that opts
in (``pytestmark = [..., pytest.mark.usefixtures("dispatch_publishes_workspace")]``).
It is opt-in, not autouse, on purpose: the modules that drive the REAL
dispatcher (``test_server.py``, ``test_transport.py``, the PAT e2e) must keep
proving that ``server.py`` publishes by itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from backend.data.rls import set_workspace_guc
from backend.data.scoping import reset_current_workspace_id, set_current_workspace_id
from backend.mcp.api import ToolContext, ToolRegistry


@pytest.fixture
def dispatch_publishes_workspace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make a direct ``ToolRegistry.call_tool`` publish like ``server._call_tool``.

    Mirrors the server exactly: bind the principal's workspace to the contextvar,
    set the GUC on the tool session's live transaction, run the tool, reset the
    contextvar. No-op GUC on SQLite.
    """
    original = ToolRegistry.call_tool

    async def _call_tool(
        self: ToolRegistry,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        ws_token = set_current_workspace_id(ctx.principal.workspace_id)
        try:
            if ctx.session is not None:
                await set_workspace_guc(await ctx.session.connection(), ctx.principal.workspace_id)
            return await original(self, name, arguments, ctx)
        finally:
            reset_current_workspace_id(ws_token)

    monkeypatch.setattr(ToolRegistry, "call_tool", _call_tool)
    yield
