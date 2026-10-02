"""A run-scoped token stops working the moment its run is finished (#1106).

Measured 2026-09-30: run ``58a7426d`` was cancelled at 10:41:01, and its Claude Code session kept
calling ``bsvibe_work_file_edit`` / ``shell_exec`` / ``file_write`` until 10:52 — the run-scoped
token was checked for *which* run, never for whether that run was still alive. A cancelled run
could go on editing its worktree.

Every work tool — the file/shell forwarders, ``ask_user_question``, ``emit_deliverable`` and the
progress trail — resolves its run through ``load_run``. That one gate is where "finished" is
refused, so no tool can be added later that forgets it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError
from backend.workflow.infrastructure.db import RunStatus

pytestmark = pytest.mark.asyncio


def _ctx(run: Any, *, workspace_id: uuid.UUID) -> ToolContext:
    class _Session:
        async def get(self, _model: Any, _pk: Any) -> Any:
            return run

    principal = McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run.id,
    )
    return ToolContext(principal=principal, session=_Session())  # type: ignore[arg-type]


class _Run:
    def __init__(self, status: RunStatus) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.status = status


@pytest.mark.parametrize("status", [RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.SHIPPED])
async def test_a_finished_run_refuses_its_token(status: RunStatus) -> None:
    from backend.mcp.tools.work_registry import load_run

    run = _Run(status)
    with pytest.raises(ToolError, match=status.value):
        await load_run(run.id, _ctx(run, workspace_id=run.workspace_id))


@pytest.mark.parametrize("status", [RunStatus.OPEN, RunStatus.RUNNING])
async def test_a_live_run_still_accepts_its_token(status: RunStatus) -> None:
    """Control: the gate must not refuse the runs that are actually working."""
    from backend.mcp.tools.work_registry import load_run

    run = _Run(status)
    assert await load_run(run.id, _ctx(run, workspace_id=run.workspace_id)) is run
