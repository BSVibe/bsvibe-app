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


# ── The MCP surface must speak the registry's contract (INV-7) ────────────────
# ``FileEditInput`` exposed ``old``/``new`` while ``ToolRegistry._file_edit`` reads
# ``old_string``/``new_string`` — so every file_edit over MCP hit
# "file_edit requires a non-empty string 'old_string'". 100% failure, and the delegation test
# below never caught it because the fake registry only RECORDED the arguments instead of
# invoking the real handler. Live proof (run 96dd7cfc): the agent's file_edit failed and it
# fell back to rewriting the whole file with file_write.


async def test_mcp_file_edit_speaks_the_registrys_argument_names(tmp_path) -> None:
    """Drive the REAL registry — a fake that only records args cannot see this class of bug."""
    from backend.mcp.tools.work_tools import FileEditInput
    from backend.workflow.infrastructure.tools import ToolRegistry

    registry = ToolRegistry(workspace_dir=tmp_path)
    registry.declared_contract = {"checks": [{"kind": "shell", "command": "pytest -q"}]}
    await registry.invoke("file_write", {"path": "a.py", "content": "x = 1\n"})

    args = FileEditInput(path="a.py", old_string="x = 1", new_string="x = 2").model_dump(
        exclude_none=False
    )
    result = await registry.invoke("file_edit", args)

    assert "edited" in result
    assert (tmp_path / "a.py").read_text() == "x = 2\n"


# ── The advertised contract must BE the registry's contract (INV-7 #2) ────────
# `DeclareVerificationInput` declared NO fields (extra="allow"), so the MCP tool advertised a
# parameterless object: the CLI had no way to know it must send `checks`, and guessed. When it
# guessed wrong the registry refused —
#
#   ToolError: declare_verification requires a non-empty 'checks' array
#
# — so the contract was never declared, EVERY file_write/file_edit was then refused (verify-first
# gate), and the agent flailed until the round cap. Measured live (run cc4ea261, 2026-07-15).
# Same class as the file_edit old/new bug: the transport paraphrased the registry instead of
# mirroring it.


async def test_mcp_declare_verification_advertises_the_checks_it_requires() -> None:
    """The agent can only send what the schema tells it to send."""
    from backend.mcp.tools.work_tools import DeclareVerificationInput

    schema = DeclareVerificationInput.model_json_schema()

    assert "checks" in schema["properties"], (
        "the tool must ADVERTISE `checks` — an empty schema makes the agent guess, and a wrong "
        "guess means the verify-first gate refuses every write for the rest of the run"
    )
    assert "checks" in schema.get("required", [])


async def test_mcp_declare_verification_round_trips_through_the_real_registry(tmp_path) -> None:
    """Drive the REAL registry with what the MCP schema produces — the only test that can see
    a transport/registry mismatch."""
    from backend.mcp.tools.work_tools import DeclareVerificationInput
    from backend.workflow.infrastructure.tools import ToolRegistry

    registry = ToolRegistry(workspace_dir=tmp_path)
    args = DeclareVerificationInput(
        checks=[{"kind": "command", "command": "uv run pytest tests/test_x.py"}]
    ).model_dump(exclude_none=False)

    result = await registry.invoke("declare_verification", args)

    assert registry.declared_contract is not None
    assert "recorded" in result.lower() or "contract" in result.lower()
    # …and the verify-first gate is now genuinely unlocked.
    await registry.invoke("file_write", {"path": "x.py", "content": "x = 1"})
    assert registry.written_paths == ["x.py"]


# ── Advertised ⇒ invokable, by construction (INV-7 #1 + #2) ───────────────────
# The class of bug: an MCP work tool ADVERTISES an ``inner`` name (and the CLI is told it may
# call it) while the run registry the loop/transport builds never registers that inner — so the
# call is ``Unknown tool``. ``knowledge_search`` was exactly this (advertised, forwarded, never
# registered on the MCP path → executor RAG grounding 0, measured live). Driving the REAL shared
# factory closes it: every forwarding work tool's inner MUST be a tool the factory registers.


async def test_every_forwarding_work_tool_maps_to_a_factory_registered_inner(tmp_path) -> None:
    """A pure declaration check: no inner name may be advertised that the factory won't build."""
    from backend.mcp.tools.work_tools import WORK_TOOL_FORWARDING_SPECS
    from backend.workflow.application.tool_registry import assemble_run_tool_registry

    registry = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None, retriever=None)
    for spec in WORK_TOOL_FORWARDING_SPECS:
        assert registry.has(spec["inner"]), (
            f"{spec['name']} forwards to inner {spec['inner']!r}, but the shared factory does "
            f"not register it — this is the advertised-but-Unknown-tool class (INV-7)"
        )


async def test_knowledge_search_actually_runs_on_the_factory_registry(tmp_path) -> None:
    """Not just registered — invokable end to end, degrading gracefully with no retriever."""
    from backend.workflow.application.tool_registry import assemble_run_tool_registry

    registry = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None, retriever=None)
    out = await registry.invoke("knowledge_search", {"query": "anything"})
    assert isinstance(out, str) and out
