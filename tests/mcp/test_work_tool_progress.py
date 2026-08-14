"""An executor turn leaves a trail while it works.

The in-process loop records an ``ExecutionRunActivity`` for EVERY tool call
(``_drive_loop.py`` — ``orch._record(run, attempt, "tool_call", …)``). An executor
agent acts through the MCP work tools instead, and those recorded nothing. So for
the production path the founder's run timeline is **blank for the whole turn** —
and a 28-minute turn that is working is indistinguishable from one that is wedged
(fix backlog #1; run ``0bbf72eb`` measured a 28-minute turn with no signal anywhere).

The signal was already there: every work-tool call runs in the API process and
commits (``persist_tool_state``). Nobody turned it into progress. These tests pin
that it now does — and that it does so with the SAME activity the native loop
writes, because chat==executor parity is the first principle here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools.work_tools import register_work_tools

pytestmark = pytest.mark.asyncio


def _principal(run_id: uuid.UUID | None) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


def _ctx(principal: McpPrincipal) -> ToolContext:
    return ToolContext(principal=principal, session=None)  # type: ignore[arg-type]


class _FakeWorkRegistry:
    """The workflow ToolRegistry bound to one run — with the written-path latch
    the real one carries (``registry.written_paths``, read by the loop's
    ``_sync_remote_tool_state``)."""

    def __init__(self, run_id: uuid.UUID, *, fail: bool = False) -> None:
        self.run_id = run_id
        self.sandbox = object()
        self.written_paths: list[str] = []
        self.calls: list[str] = []
        self._fail = fail

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        if self._fail:
            raise RuntimeError("the tool blew up")
        if name in {"file_write", "file_edit"}:
            self.written_paths.append(str(arguments.get("path")))
        return "ok"


def _build(
    *, fail: bool = False, on_progress: Any = None
) -> tuple[ToolRegistry, list[dict[str, Any]], dict[uuid.UUID, _FakeWorkRegistry]]:
    recorded: list[dict[str, Any]] = []
    built: dict[uuid.UUID, _FakeWorkRegistry] = {}

    async def _registry_for_run(run_id: uuid.UUID, ctx: ToolContext) -> _FakeWorkRegistry:
        built.setdefault(run_id, _FakeWorkRegistry(run_id, fail=fail))
        return built[run_id]

    async def _record_progress(
        run_id: uuid.UUID, ctx: ToolContext, payload: dict[str, Any]
    ) -> None:
        if on_progress is not None:
            await on_progress()
        recorded.append({"run_id": run_id, **payload})

    async def _noop(*_a: Any, **_k: Any) -> str:
        return "ok"

    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=_registry_for_run,
        record_question=_noop,
        record_deliverable=_noop,
        persist_state=_noop,
        record_progress=_record_progress,
    )
    return reg, recorded, built


# ── the trail exists at all ─────────────────────────────────────────────────


async def test_a_work_tool_call_records_progress_while_the_turn_is_still_running() -> None:
    reg, recorded, _ = _build()
    run_id = uuid.uuid4()

    await reg.call_tool("bsvibe_work_file_read", {"path": "a.py"}, _ctx(_principal(run_id)))

    assert len(recorded) == 1
    assert recorded[0]["run_id"] == run_id
    assert recorded[0]["tool"] == "file_read"
    assert recorded[0]["ok"] is True


async def test_a_read_only_call_still_leaves_a_row() -> None:
    """It drops out of the founder's timeline as noise (``_tool_call_label`` returns
    None without writes) — but it is the difference between "working" and "wedged",
    so it must still be written."""
    reg, recorded, _ = _build()

    await reg.call_tool("bsvibe_work_file_list", {"path": "."}, _ctx(_principal(uuid.uuid4())))

    assert [r["tool"] for r in recorded] == ["file_list"]
    assert recorded[0]["writes"] == []


async def test_every_call_leaves_its_own_row_so_the_trail_advances() -> None:
    reg, recorded, _ = _build()
    ctx = _ctx(_principal(uuid.uuid4()))

    await reg.call_tool("bsvibe_work_file_read", {"path": "a.py"}, ctx)
    await reg.call_tool("bsvibe_work_shell_exec", {"command": "pytest -q"}, ctx)
    await reg.call_tool("bsvibe_work_file_read", {"path": "b.py"}, ctx)

    assert [r["tool"] for r in recorded] == ["file_read", "shell_exec", "file_read"]


# ── parity with the native loop ─────────────────────────────────────────────


async def test_a_write_carries_its_paths_so_the_timeline_can_say_delivered() -> None:
    """``_tool_call_label`` builds "Delivered X" from ``payload["writes"]``. Without
    the paths the executor's work never appears on the timeline at all."""
    reg, recorded, _ = _build()

    await reg.call_tool(
        "bsvibe_work_file_write",
        {"path": "backend/new.py", "content": "x = 1"},
        _ctx(_principal(uuid.uuid4())),
    )

    assert recorded[0]["writes"] == ["backend/new.py"]


async def test_only_the_paths_this_call_wrote_are_reported() -> None:
    """The registry's latch ACCUMULATES across a run. Reporting the whole latch each
    time would re-announce every earlier file on every later call."""
    reg, recorded, _ = _build()
    ctx = _ctx(_principal(uuid.uuid4()))

    await reg.call_tool("bsvibe_work_file_write", {"path": "a.py", "content": "1"}, ctx)
    await reg.call_tool("bsvibe_work_file_write", {"path": "b.py", "content": "2"}, ctx)

    assert recorded[0]["writes"] == ["a.py"]
    assert recorded[1]["writes"] == ["b.py"]


async def test_a_failing_tool_is_recorded_as_a_failure_and_still_raises() -> None:
    """The native loop records ``ok`` both ways (``_invoke_tool_safely`` catches).
    A tool that fails silently is exactly the invisibility this closes — and the
    agent must still receive the error."""
    reg, recorded, _ = _build(fail=True)

    # The registry wraps a handler exception into the ToolError the agent receives.
    with pytest.raises(ToolError, match="failed: RuntimeError"):
        await reg.call_tool(
            "bsvibe_work_file_read", {"path": "a.py"}, _ctx(_principal(uuid.uuid4()))
        )

    assert len(recorded) == 1
    assert recorded[0]["ok"] is False


# ── observability must never cost the agent its work ────────────────────────


async def test_a_broken_recorder_does_not_break_the_agents_work() -> None:
    async def _boom() -> None:
        raise RuntimeError("activity table is on fire")

    reg, _, built = _build(on_progress=_boom)
    run_id = uuid.uuid4()

    out = await reg.call_tool(
        "bsvibe_work_file_write", {"path": "a.py", "content": "1"}, _ctx(_principal(run_id))
    )

    assert out["result"] == "ok"
    assert built[run_id].written_paths == ["a.py"]


# ── never registered-but-dead ───────────────────────────────────────────────


async def test_the_work_surface_cannot_be_registered_without_a_recorder() -> None:
    """A half-wired subsystem is the failure mode this codebase keeps hitting: the
    visible half ships, the producer is never wired, and unit tests stay green. Make
    the composition root unable to forget."""
    reg = ToolRegistry()

    async def _noop(*_a: Any, **_k: Any) -> str:
        return "ok"

    async def _registry_for_run(run_id: uuid.UUID, ctx: ToolContext) -> _FakeWorkRegistry:
        return _FakeWorkRegistry(run_id)

    with pytest.raises(TypeError):
        register_work_tools(  # type: ignore[call-arg]
            reg,
            registry_for_run=_registry_for_run,
            record_question=_noop,
            record_deliverable=_noop,
            persist_state=_noop,
        )


async def test_the_composition_root_gate_is_load_bearing() -> None:
    """``register_all_tools`` registers the work surface only when EVERY loop-owned
    effect is injected. Omitting the recorder must drop the surface loudly (an agent
    gets "unknown tool") rather than register a run that silently leaves no trail —
    the half-wired shape this codebase keeps rediscovering.
    """
    from backend.mcp.tools import register_all_tools

    async def _noop(*_a: Any, **_k: Any) -> str:
        return "ok"

    without = ToolRegistry()
    register_all_tools(without, record_question=_noop, record_deliverable=_noop)
    assert "bsvibe_work_file_write" not in set(without.names())

    with_it = ToolRegistry()
    register_all_tools(
        with_it, record_question=_noop, record_deliverable=_noop, record_progress=_noop
    )
    assert "bsvibe_work_file_write" in set(with_it.names())
