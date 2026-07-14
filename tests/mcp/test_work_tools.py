"""Work tools over MCP — the executor's remote hands (T1).

BSVibe's first principle: an executor account and a LiteLLM account behave IDENTICALLY
through ``chat()``. The founder's clarification of it: **the executor is the user's LLM
client, not an execution environment.** State lives on the SERVER; an agent acts on it
through BSVibe's own tools, executed server-side — never through the CLI's own local tools
in a temp dir the worker then scrapes.

That scraping model is what produced, in this order (2026-07-14 parity audit):

* an agent that could not see the product's code at all (no ``repo_url`` → a bare mkdtemp)
  and so **invented** it — captured, committed, merged into the founder's main;
* whole-file capture overwriting the server worktree from a stale baseline, silently
  reverting previously shipped work;
* ``raw = b"" if truncated`` → a file over 256 KB **zeroed** in the founder's repo;
* a file deletion 422-ing the entire result (the worker sends ``deleted: True``; the
  receiving schema is ``extra="forbid"``), so the run dies on a timeout with no reason.

These tools delegate to the SAME :class:`~backend.workflow.infrastructure.tools.ToolRegistry`
the in-process (LiteLLM) loop already calls — one implementation, two transports. The MCP
layer adds exactly one thing: **run scoping**.

THE invariant: the run comes from the TOKEN, never from the tool arguments. A token minted
for run A cannot touch run B, whatever the agent sends.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools.work_tools import register_work_tools

pytestmark = pytest.mark.asyncio


def _principal(
    *,
    run_id: uuid.UUID | None,
    scopes: tuple[str, ...] = ("mcp:read", "mcp:write"),
) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="bsvibe-worker",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


def _ctx(principal: McpPrincipal) -> ToolContext:
    return ToolContext(principal=principal, session=None)  # type: ignore[arg-type]


async def _unused_effect(*_a: Any, **_k: Any) -> str:
    """The two loop-owned effects are covered in test_work_tools_ask_and_deliver.py."""
    return "ok"


class _FakeWorkRegistry:
    """Stands in for the workflow ToolRegistry bound to ONE run."""

    def __init__(self, run_id: uuid.UUID, *, sandbox: object | None = object()) -> None:
        self.run_id = run_id
        self.sandbox = sandbox
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return f"{name} ran in run {self.run_id}"


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    built: dict[uuid.UUID, _FakeWorkRegistry] = {}

    async def _registry_for_run(run_id: uuid.UUID, ctx: ToolContext) -> _FakeWorkRegistry:
        built.setdefault(run_id, _FakeWorkRegistry(run_id))
        return built[run_id]

    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=_registry_for_run,
        record_question=_unused_effect,
        record_deliverable=_unused_effect,
        persist_state=_unused_effect,
    )
    reg.built = built  # type: ignore[attr-defined]
    return reg


# ── the surface ─────────────────────────────────────────────────────────────


async def test_the_whole_tool_schema_is_exposed(registry: ToolRegistry) -> None:
    """Audit #20: today an executor can only ever return ``declare_verification`` — every
    other tool is unreachable, so an executor-driven run can never ask the founder a mid-run
    question and never emits a mid-run deliverable. Moving FILE state server-side does not
    fix that on its own: the surface has to carry the whole schema, or the asymmetry becomes
    permanent."""
    assert {
        "bsvibe_work_file_read",
        "bsvibe_work_file_list",
        "bsvibe_work_file_write",
        "bsvibe_work_file_edit",
        "bsvibe_work_shell_exec",
        "bsvibe_work_declare_verification",
        "bsvibe_work_knowledge_search",
    } <= set(registry.names())


@pytest.mark.parametrize(
    ("tool", "args", "inner"),
    [
        ("bsvibe_work_file_read", {"path": "backend/a.py"}, "file_read"),
        ("bsvibe_work_file_list", {"path": "."}, "file_list"),
        ("bsvibe_work_file_write", {"path": "a.py", "content": "x = 1"}, "file_write"),
        ("bsvibe_work_shell_exec", {"command": "uv run pytest -q"}, "shell_exec"),
    ],
)
async def test_tool_delegates_to_the_run_bound_registry(
    registry: ToolRegistry, tool: str, args: dict[str, Any], inner: str
) -> None:
    """The MCP layer is a transport, not a second implementation: it forwards to the SAME
    registry the in-process loop calls, bound to the principal's run."""
    run_id = uuid.uuid4()

    out = await registry.call_tool(tool, args, _ctx(_principal(run_id=run_id)))

    work = registry.built[run_id]  # type: ignore[attr-defined]
    assert work.calls == [(inner, args)]
    assert str(run_id) in out["result"]


# ── the invariant: the run comes from the TOKEN ─────────────────────────────


async def test_a_run_scoped_token_is_required(registry: ToolRegistry) -> None:
    """The founder's own workspace-wide MCP token (the one in their editor) must NOT be able
    to write into a run's worktree. These tools exist for the executor's per-task token."""
    with pytest.raises(ToolError, match="run"):
        await registry.call_tool(
            "bsvibe_work_file_write",
            {"path": "a.py", "content": "x"},
            _ctx(_principal(run_id=None)),
        )


async def test_run_id_cannot_be_passed_as_an_argument(registry: ToolRegistry) -> None:
    """The blast radius of a leaked worker token is ONE run. An agent that tries to name a
    different run is refused by the schema itself (``extra="forbid"``) — the run is read from
    the principal and an argument can never redirect it."""
    mine, other = uuid.uuid4(), uuid.uuid4()

    with pytest.raises(ToolError):
        await registry.call_tool(
            "bsvibe_work_file_write",
            {"path": "a.py", "content": "x", "run_id": str(other)},
            _ctx(_principal(run_id=mine)),
        )

    assert other not in registry.built  # type: ignore[attr-defined]


async def test_writing_requires_the_write_scope(registry: ToolRegistry) -> None:
    """A read-only token reads; it does not edit the founder's code."""
    with pytest.raises(ToolError):
        await registry.call_tool(
            "bsvibe_work_file_write",
            {"path": "a.py", "content": "x"},
            _ctx(_principal(run_id=uuid.uuid4(), scopes=("mcp:read",))),
        )


async def test_reading_does_not_require_the_write_scope(registry: ToolRegistry) -> None:
    run_id = uuid.uuid4()

    out = await registry.call_tool(
        "bsvibe_work_file_read",
        {"path": "a.py"},
        _ctx(_principal(run_id=run_id, scopes=("mcp:read",))),
    )

    assert "file_read" in out["result"]


async def test_shell_exec_is_refused_when_the_run_has_no_sandbox() -> None:
    """``ToolRegistry`` falls back to a HOST subprocess when it holds no sandbox session — and
    on this transport the host is the API container. A run without a sandbox must therefore be
    refused loudly, not served quietly (the "sandbox disabled → silent host fallback" trap this
    codebase has already been bitten by once)."""
    run_id = uuid.uuid4()

    async def _registry_for_run(rid: uuid.UUID, ctx: ToolContext) -> _FakeWorkRegistry:
        return _FakeWorkRegistry(rid, sandbox=None)

    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=_registry_for_run,
        record_question=_unused_effect,
        record_deliverable=_unused_effect,
        persist_state=_unused_effect,
    )

    with pytest.raises(ToolError, match="sandbox"):
        await reg.call_tool(
            "bsvibe_work_shell_exec", {"command": "rm -rf /"}, _ctx(_principal(run_id=run_id))
        )


async def test_file_tools_still_work_without_a_sandbox() -> None:
    """Files live on the server's disk either way — only SHELL needs the box."""
    run_id = uuid.uuid4()

    async def _registry_for_run(rid: uuid.UUID, ctx: ToolContext) -> _FakeWorkRegistry:
        return _FakeWorkRegistry(rid, sandbox=None)

    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=_registry_for_run,
        record_question=_unused_effect,
        record_deliverable=_unused_effect,
        persist_state=_unused_effect,
    )

    out = await reg.call_tool(
        "bsvibe_work_file_read", {"path": "a.py"}, _ctx(_principal(run_id=run_id))
    )
    assert "file_read" in out["result"]
