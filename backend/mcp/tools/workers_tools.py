"""Workers MCP tools — UI parity for Settings → Workers (Lift E4).

The PWA's Workers tab exposes two founder-facing operations:

* Listing registered executor workers (name / capabilities / status /
  last-heartbeat / created-at).
* Revoking a worker — soft-delete that detaches the worker from the
  workspace's executor model accounts. The daemon's next heartbeat 401s and
  the loop exits gracefully.

The MCP surface mirrors these one-for-one so a founder operating through
Claude Code can self-host the worker pool end-to-end without the PWA.
Following the bsvibe-mcp-ui-parity feedback: each UI verb has a tool, and
``workers_revoke`` is a mutation → ``mcp:write`` scope (matching
``model_accounts_delete`` rather than ``mcp:admin`` — workers are routinely
rotated, they aren't an admin-only destructive surface).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel

from backend.executors import service
from backend.executors.db import WorkerRow
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


def _row_to_dict(row: WorkerRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "name": row.name,
        "labels": list(row.labels or []),
        "capabilities": list(row.capabilities or []),
        "status": row.status,
        "is_active": row.is_active,
        "last_heartbeat": row.last_heartbeat.isoformat() if row.last_heartbeat else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# bsvibe_workers_list
# ---------------------------------------------------------------------------
class WorkersListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(args: WorkersListInput, ctx: ToolContext) -> Any:  # noqa: ARG001
    rows = await service.list_workers(ctx.session, ctx.principal.workspace_id)
    return _Envelope([_row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_workers_revoke
# ---------------------------------------------------------------------------
class WorkersRevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: uuid.UUID


class WorkersRevokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revoked: bool
    worker_id: str


async def _h_revoke(args: WorkersRevokeInput, ctx: ToolContext) -> Any:
    row = await service.revoke_worker(
        ctx.session,
        workspace_id=ctx.principal.workspace_id,
        worker_id=args.worker_id,
    )
    if row is None:
        raise ToolError(f"worker not found: {args.worker_id}")
    await ctx.session.commit()
    return WorkersRevokeOutput(revoked=True, worker_id=str(args.worker_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_workers_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_workers_list",
            description=(
                "List registered executor workers in the active workspace — "
                "the founder's host machines running CLI executors "
                "(claude_code / codex / opencode). Mirrors the PWA Settings → "
                "Workers tab."
            ),
            input_schema=WorkersListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_workers_revoke",
            description=(
                "Revoke a worker by id. The worker daemon's next heartbeat "
                "401s and the loop exits gracefully. Use this to rotate a "
                "lost / compromised host token, then run "
                "`bsvibe-worker register` again on the host."
            ),
            input_schema=WorkersRevokeInput,
            output_schema=WorkersRevokeOutput,
            handler=_h_revoke,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.workers_revoke.invoked",
        )
    )


__all__ = ["register_workers_tools"]
