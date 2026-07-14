"""The production seam behind the MCP work tools: run → the loop's own ToolRegistry.

This is what keeps "one implementation, two transports" true. The factory resolves a run to
exactly what the in-process loop binds:

    workspace_dir = the run's SERVER-SIDE worktree      (not a temp dir on the user's machine)
    sandbox       = the run's DinD session               (the same box verification runs in)

and hands back a :class:`~backend.workflow.infrastructure.tools.ToolRegistry`. Nothing here
re-implements a tool; the MCP layer only transports.

Security invariant: the run is read from the token, and the run must belong to the token's
workspace. A run-scoped token from workspace A must not reach a run in workspace B even if
someone hands it that run's id.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError
from backend.workflow.infrastructure.tools import ToolError as WorkToolError

pytestmark = pytest.mark.asyncio


def _principal(*, run_id: uuid.UUID, workspace_id: uuid.UUID) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


class _FakeRuns:
    def __init__(self, run: Any) -> None:
        self._run = run

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._run


class _Run:
    def __init__(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self.id = run_id
        self.workspace_id = workspace_id
        self.product_id = uuid.uuid4()
        # The run carries the work tools' per-run latches between MCP calls (T2b-2).
        self.payload: dict[str, object] = {}


async def test_registry_is_bound_to_the_runs_server_side_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's file tools must land in the run's worktree ON THE SERVER — the whole point
    of the redesign. Prove the registry the MCP layer hands back writes there."""
    from backend.mcp.tools import work_registry

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    worktree = tmp_path / "runs" / str(run_id)
    worktree.mkdir(parents=True)
    monkeypatch.setattr(
        work_registry, "run_worktree_path", lambda rid: tmp_path / "runs" / str(rid)
    )
    monkeypatch.setattr(work_registry, "_sandbox_for", _no_sandbox)

    registry = await work_registry.build_run_tool_registry(
        run_id,
        ToolContext(  # type: ignore[arg-type]
            principal=_principal(run_id=run_id, workspace_id=ws),
            session=_FakeRuns(_Run(run_id, ws)),
        ),
    )
    await registry.invoke(
        "declare_verification",
        {"checks": [{"kind": "shell", "command": "python -c 'import hello'"}]},
    )
    await registry.invoke("file_write", {"path": "hello.py", "content": "x = 1"})

    assert (worktree / "hello.py").read_text() == "x = 1"


async def test_the_verify_first_gate_applies_to_the_executor_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit #7: on LiteLLM, ``file_write`` RAISES until ``declare_verification`` has been
    called — it is structurally impossible to have written code with no contract. The
    executor writes with its own CLI tools, so the gate never applies to it: a founder's TDD
    guarantee depends on which account they routed.

    Going through the registry restores it — the gate is in the registry, and now the executor
    goes through the registry."""
    from backend.mcp.tools import work_registry

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    (tmp_path / "runs" / str(run_id)).mkdir(parents=True)
    monkeypatch.setattr(
        work_registry, "run_worktree_path", lambda rid: tmp_path / "runs" / str(rid)
    )
    monkeypatch.setattr(work_registry, "_sandbox_for", _no_sandbox)

    registry = await work_registry.build_run_tool_registry(
        run_id,
        ToolContext(  # type: ignore[arg-type]
            principal=_principal(run_id=run_id, workspace_id=ws),
            session=_FakeRuns(_Run(run_id, ws)),
        ),
    )

    with pytest.raises(WorkToolError):
        await registry.invoke("file_write", {"path": "a.py", "content": "x"})


async def test_a_run_in_another_workspace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-tenant guard: the token says workspace A; the run belongs to workspace B."""
    from backend.mcp.tools import work_registry

    run_id = uuid.uuid4()
    monkeypatch.setattr(work_registry, "run_worktree_path", lambda rid: tmp_path / str(rid))
    monkeypatch.setattr(work_registry, "_sandbox_for", _no_sandbox)

    with pytest.raises(ToolError, match="workspace"):
        await work_registry.build_run_tool_registry(
            run_id,
            ToolContext(  # type: ignore[arg-type]
                principal=_principal(run_id=run_id, workspace_id=uuid.uuid4()),
                session=_FakeRuns(_Run(run_id, uuid.uuid4())),
            ),
        )


async def test_an_unknown_run_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.mcp.tools import work_registry

    monkeypatch.setattr(work_registry, "run_worktree_path", lambda rid: tmp_path / str(rid))
    monkeypatch.setattr(work_registry, "_sandbox_for", _no_sandbox)

    with pytest.raises(ToolError, match="run"):
        await work_registry.build_run_tool_registry(
            uuid.uuid4(),
            ToolContext(  # type: ignore[arg-type]
                principal=_principal(run_id=uuid.uuid4(), workspace_id=uuid.uuid4()),
                session=_FakeRuns(None),
            ),
        )


async def _no_sandbox(_run: Any, _workspace_dir: Path) -> None:
    """Tool handlers accept ``sandbox=None`` (host execution is refused at that seam)."""
    return None
