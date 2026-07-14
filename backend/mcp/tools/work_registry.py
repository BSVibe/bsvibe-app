"""Run → the loop's own ToolRegistry. The production seam behind the MCP work tools.

"One implementation, two transports" only holds if the MCP layer binds the SAME registry the
in-process loop binds. That is all this module does:

    workspace_dir = the run's SERVER-SIDE worktree   (never a temp dir on the user's machine)
    sandbox       = the run's DinD session            (the same box verification runs in)

Two guards, both about blast radius:

* the run comes from the TOKEN (:attr:`McpPrincipal.run_id`), enforced in
  :mod:`backend.mcp.tools.work_tools`;
* the run must belong to the token's WORKSPACE — a run-scoped token from workspace A cannot
  reach a run in workspace B even if it is handed that run's id.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog

from backend.mcp.api import ToolContext, ToolError
from backend.storage.product_workspace import run_worktree_path
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import build_sandbox_manager
from backend.workflow.infrastructure.tools import ToolRegistry

logger = structlog.get_logger(__name__)


async def _sandbox_for(run: ExecutionRun, workspace_dir: Path) -> Any:
    """The run's sandbox session — the same box the verifier runs in.

    ``SandboxManager.acquire`` is per-PROJECT and reused across runs, so the API process can
    attach to the session the worker's loop is already using; no cross-process RPC. A run with
    no product (substrate-only) gets no sandbox: the tool layer then refuses ``shell_exec``
    rather than silently running it on the host.
    """
    if run.product_id is None:
        return None
    manager = build_sandbox_manager()
    if manager is None:
        return None
    return await manager.acquire(run.product_id, str(workspace_dir))


async def build_run_tool_registry(run_id: uuid.UUID, ctx: ToolContext) -> ToolRegistry:
    """Bind the workflow ToolRegistry to ``run_id``'s server-side worktree + sandbox."""
    run = await ctx.session.get(ExecutionRun, run_id)
    if run is None:
        raise ToolError(f"run {run_id} does not exist")
    if run.workspace_id != ctx.principal.workspace_id:
        # Defense in depth: the token is already run-scoped, but a run must never be
        # reachable from another workspace's token.
        logger.warning(
            "mcp_work_run_workspace_mismatch",
            run_id=str(run_id),
            token_workspace=str(ctx.principal.workspace_id),
        )
        raise ToolError("this run belongs to another workspace")

    workspace_dir = run_worktree_path(run_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    sandbox = await _sandbox_for(run, workspace_dir)
    return ToolRegistry(workspace_dir=workspace_dir, sandbox=sandbox)


__all__ = ["build_run_tool_registry"]
