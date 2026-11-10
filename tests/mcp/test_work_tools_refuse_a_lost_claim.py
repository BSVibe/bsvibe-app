"""A run-scoped token dies with the CLAIM it was minted under.

Measured 2026-11-10, run ``f33f959b``: the worker was restarted mid-run. ``stop()`` released the
run (RUNNING -> OPEN) and a new worker claimed it within a second, but the executor turn the old
job had launched kept calling ``bsvibe_work_file_edit`` -- the token was only ever checked for
*which* run, never for whether the job that minted it still held that run.

These tests go through the production seam (``build_run_tool_registry`` -> ``load_run``) with a
real worktree, so a refused ``file_edit`` is proven by the file on disk, not by a mock.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import work_registry
from backend.mcp.tools.work_tools import register_work_tools
from backend.workflow.infrastructure.db import RunStatus

pytestmark = pytest.mark.asyncio

_OLD_JOB = uuid.uuid4()
_NEW_JOB = uuid.uuid4()


class _Run:
    def __init__(self, workspace_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = workspace_id
        self.product_id = uuid.uuid4()
        self.status = RunStatus.RUNNING
        self.claimed_by: uuid.UUID | None = _OLD_JOB
        self.payload: dict[str, object] = {}


class _Session:
    def __init__(self, run: _Run) -> None:
        self._run = run

    async def get(self, _model: Any, _pk: Any, **_kw: Any) -> Any:
        return self._run

    async def commit(self) -> None:
        return None


async def _no_sandbox(*_a: Any, **_k: Any) -> None:
    return None


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(work_registry, "run_worktree_path", lambda rid: tmp_path / str(rid))
    monkeypatch.setattr(work_registry, "_sandbox_for", _no_sandbox)
    return tmp_path


@pytest.fixture
def tools() -> ToolRegistry:
    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=work_registry.build_run_tool_registry,
        record_question=_noop,
        record_deliverable=_noop,
        persist_state=work_registry.persist_tool_state,
        record_progress=_noop,
    )
    return reg


def _ctx(run: _Run, *, claim: uuid.UUID | None) -> ToolContext:
    principal = McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=run.workspace_id,
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run.id,
        run_claim=claim,
    )
    return ToolContext(principal=principal, session=_Session(run))  # type: ignore[arg-type]


def _seed(worktree: Path, run: _Run) -> Path:
    target = worktree / str(run.id) / "a.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    return target


_DECLARE = {"checks": [{"kind": "command", "command": "true"}]}
_EDIT = {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}


async def test_the_claim_holder_still_edits(worktree: Path, tools: ToolRegistry) -> None:
    """Control: the guard must not refuse the job that actually holds the run."""
    run = _Run(uuid.uuid4())
    target = _seed(worktree, run)
    ctx = _ctx(run, claim=_OLD_JOB)

    await tools.call_tool("bsvibe_work_declare_verification", _DECLARE, ctx)
    await tools.call_tool("bsvibe_work_file_read", {"path": "a.py"}, ctx)
    await tools.call_tool("bsvibe_work_file_edit", _EDIT, ctx)

    assert target.read_text() == "x = 2\n"


async def test_a_released_run_refuses_the_old_token(worktree: Path, tools: ToolRegistry) -> None:
    """(a) The job let go (RUNNING -> OPEN): the turn it launched is told to stop."""
    run = _Run(uuid.uuid4())
    target = _seed(worktree, run)
    ctx = _ctx(run, claim=_OLD_JOB)
    await tools.call_tool("bsvibe_work_declare_verification", _DECLARE, ctx)
    await tools.call_tool("bsvibe_work_file_read", {"path": "a.py"}, ctx)

    run.status = RunStatus.OPEN
    run.claimed_by = None

    with pytest.raises(ToolError, match="no longer held"):
        await tools.call_tool("bsvibe_work_file_edit", _EDIT, ctx)
    assert target.read_text() == "x = 1\n"


async def test_a_reclaimed_run_refuses_the_old_token(worktree: Path, tools: ToolRegistry) -> None:
    """(b) Another job claimed the run again: it is RUNNING, but not under THIS token's claim."""
    run = _Run(uuid.uuid4())
    target = _seed(worktree, run)
    ctx = _ctx(run, claim=_OLD_JOB)
    await tools.call_tool("bsvibe_work_declare_verification", _DECLARE, ctx)
    await tools.call_tool("bsvibe_work_file_read", {"path": "a.py"}, ctx)

    run.status = RunStatus.OPEN
    run.claimed_by = None
    run.status = RunStatus.RUNNING
    run.claimed_by = _NEW_JOB

    with pytest.raises(ToolError, match="no longer held"):
        await tools.call_tool("bsvibe_work_file_edit", _EDIT, ctx)
    assert target.read_text() == "x = 1\n"

    # ... while the new job's own token works on the same run.
    new_ctx = _ctx(run, claim=_NEW_JOB)
    await tools.call_tool("bsvibe_work_declare_verification", _DECLARE, new_ctx)
    await tools.call_tool("bsvibe_work_file_read", {"path": "a.py"}, new_ctx)
    await tools.call_tool("bsvibe_work_file_edit", _EDIT, new_ctx)
    assert target.read_text() == "x = 2\n"
