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

from backend.config import get_settings
from backend.mcp.api import ToolContext, ToolError
from backend.storage.product_workspace import run_worktree_path
from backend.workflow.application.tool_registry import (
    WORK_TOOL_STATE_KEY as _WORK_TOOL_STATE_KEY,
)
from backend.workflow.application.tool_registry import (
    assemble_run_tool_registry,
)
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import get_sandbox_manager
from backend.workflow.infrastructure.tools import ToolRegistry

logger = structlog.get_logger(__name__)

#: Where the run carries the work tools' per-run latches between MCP calls.
#: Re-exported — the key is a property of the RUN and lives in the workflow layer, so the loop
#: (which cannot import ``backend.mcp``) reads the same state this transport writes.
WORK_TOOL_STATE_KEY = _WORK_TOOL_STATE_KEY


async def _sandbox_for(run: ExecutionRun, workspace_dir: Path) -> Any:
    """The run's sandbox session — the same box the verifier runs in.

    Resolved through the process singleton (``get_sandbox_manager``), NOT a fresh build: this
    transport is invoked once per MCP tool call, and a per-call manager carries an empty
    container cache, so ``acquire`` would tear down (``docker rm -f``) and recreate the
    per-project container on EVERY ``file_read`` — the 300s-timeout tax, and it kills the
    container a parallel run is verifying in. The singleton keeps one cache for the process, so
    the container is created once and reused across a run's tool calls. A run with no product
    (substrate-only) gets no sandbox: the tool layer then refuses ``shell_exec`` rather than
    silently running it on the host.
    """
    if run.product_id is None:
        return None
    manager = get_sandbox_manager()
    if manager is None:
        return None
    return await manager.acquire(run.product_id, str(workspace_dir))


async def load_run(run_id: uuid.UUID, ctx: ToolContext) -> ExecutionRun:
    """The run this token may act on — with the cross-tenant guard.

    Shared by every work tool, including the two that touch no files
    (``ask_user_question`` / ``emit_deliverable``), so the guard cannot drift between them.
    """
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
    return run


async def _retriever_for(run: ExecutionRun, ctx: ToolContext) -> Any:
    """The run's canon retriever, built IN the API process for ``knowledge_search``.

    INV-7 #1: the executor's ``knowledge_search`` used to be advertised by the MCP layer and
    forwarded to inner ``knowledge_search`` — which this transport's bare registry never
    registered, so every call was ``Unknown tool`` and the agent's RAG grounding was 0. The
    retriever's construction chain (``backend.knowledge``) is inside the MCP context's import
    allowlist, so the transport can materialise the SAME retriever the in-process loop uses
    (:func:`backend.knowledge.retrieval.answer_grounding.build_canon_retriever`) without reaching
    a forbidden context. A build failure degrades to ``None`` (the handler answers "no knowledge")
    — it must never break the run's whole tool surface.
    """
    from backend.knowledge.retrieval.answer_grounding import (  # noqa: PLC0415 — lazy heavy import
        build_canon_retriever,
    )

    try:
        return build_canon_retriever(
            ctx.session, settings=get_settings(), workspace_id=run.workspace_id
        )
    except Exception:  # noqa: BLE001 — a retriever build failure must not sink the tool surface
        logger.warning("mcp_knowledge_retriever_unavailable", run_id=str(run.id), exc_info=True)
        return None


async def build_run_tool_registry(run_id: uuid.UUID, ctx: ToolContext) -> ToolRegistry:
    """Bind the workflow ToolRegistry to ``run_id``'s server-side worktree + sandbox.

    Uses the SAME factory the in-process loop calls
    (:func:`backend.workflow.application.tool_registry.assemble_run_tool_registry`) so the base
    tools + ``knowledge_search`` cannot drift between the two transports (INV-7 #1)."""
    run = await load_run(run_id, ctx)

    workspace_dir = run_worktree_path(run_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    sandbox = await _sandbox_for(run, workspace_dir)
    registry = assemble_run_tool_registry(
        workspace_dir=workspace_dir,
        sandbox=sandbox,
        retriever=await _retriever_for(run, ctx),
    )
    # The registry's latches (the declared verification contract; the paths the agent grounded
    # itself in) belong to the RUN, not to this per-request object. The in-process loop keeps
    # one registry alive for a whole run; this transport builds a new one per MCP call, so
    # without rehydrating, an agent would declare its contract and then be told, on the very
    # next call, to declare its contract (measured on the live surface, 2026-07-14).
    registry.restore_state(((run.payload or {}).get(WORK_TOOL_STATE_KEY)) or None)
    return registry


async def persist_tool_state(run_id: uuid.UUID, ctx: ToolContext, registry: ToolRegistry) -> None:
    """Write the registry's per-run latches back onto the run, after a tool call.

    COMMIT, not flush: the MCP dispatcher opens the request session and never commits it
    (``backend/mcp/server.py``), so every write a handler leaves uncommitted is rolled back
    when the request ends. Each MCP write tool commits for itself — that is the convention
    here, and a flush-only handler silently does nothing (measured on the live surface,
    2026-07-14: the agent declared its contract and was still refused the write).
    """
    # MERGE under a row lock — never blind-overwrite.
    #
    # The CLI issues tool calls in PARALLEL batches. Each request built its registry from its own
    # snapshot of ``run.payload`` and wrote the WHOLE payload back, so two overlapping calls
    # read-modify-wrote each other away:
    #
    #   declare_verification: reads {} -> writes {contract: X}
    #   file_list (started before that committed): reads {} -> writes {contract: None}   ← clobbers
    #
    # Measured live (run 3e163fc5): the agent declared its contract AND wrote files, yet the run's
    # state came out empty — so the loop saw no work done, nudged, and re-dispatched the agent in
    # a loop. ``with_for_update`` serialises the read-modify-write; the merge makes a stale call
    # additive instead of destructive.
    run = await ctx.session.get(ExecutionRun, run_id, with_for_update=True)
    if run is None:
        return
    payload = dict(run.payload or {})
    payload[WORK_TOOL_STATE_KEY] = _merge_work_tool_state(
        current=payload.get(WORK_TOOL_STATE_KEY) or {},
        incoming=registry.export_state(),
    )
    run.payload = payload
    await ctx.session.commit()


def _merge_work_tool_state(*, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Fold one call's registry state into the run's, additively.

    A call that knows nothing (its registry was built from a stale snapshot) must not erase what
    a concurrent call already recorded — so every field is "keep what we have unless this call
    has something", and the path lists are UNIONS.
    """

    def _union(a: Any, b: Any) -> list[str]:
        out: list[str] = []
        for seq in (a or [], b or []):
            for item in seq:
                text = str(item)
                if text not in out:
                    out.append(text)
        return out

    return {
        "declared_contract": incoming.get("declared_contract") or current.get("declared_contract"),
        "declared_knowledge": (
            incoming.get("declared_knowledge") or current.get("declared_knowledge")
        ),
        "grounded_paths": sorted(
            set(_union(current.get("grounded_paths"), incoming.get("grounded_paths")))
        ),
        "written_paths": _union(current.get("written_paths"), incoming.get("written_paths")),
    }


__all__ = ["WORK_TOOL_STATE_KEY", "build_run_tool_registry", "load_run", "persist_tool_state"]
