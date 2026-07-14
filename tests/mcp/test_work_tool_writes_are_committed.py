"""Every MCP work write COMMITS — the dispatcher will not do it for you (T2b-3).

``build_server`` opens the request session and never commits it: each write tool commits for
itself. That convention is invisible until you break it, and breaking it fails **silently** —
the handler returns success and the row vanishes when the request ends.

Measured on the live surface, 2026-07-14:

    declare_verification  → "verification contract recorded"   ✅ (returned success)
    file_write            → "declare your verification first"  ❌ (the contract was gone)

The same flush-only bug was sitting in ``ask_user_question`` and ``emit_deliverable``: the
founder would never have been asked, and the deliverable would never have appeared, with both
tools reporting success.

So: the tests below assert the COMMIT, not the write. A test that only checks the object in
the same session passes happily while production loses every row.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Session:
    """Records the lifecycle calls; that is the whole contract under test."""

    def __init__(self, run: Any) -> None:
        self._run = run
        self.committed = False
        self.flushed = False
        self.locked = False

    async def get(self, _model: Any, _pk: Any, with_for_update: bool = False) -> Any:
        # The real ``AsyncSession.get`` takes ``with_for_update`` — persist_tool_state ROW-LOCKS
        # the run so parallel tool calls cannot read-modify-write each other's state away. A
        # double that does not accept it hides that the production call even happens.
        self.locked = with_for_update
        return self._run

    async def commit(self) -> None:
        self.committed = True

    async def flush(self) -> None:
        self.flushed = True


class _Ctx:
    def __init__(self, session: _Session, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.principal = type("P", (), {"workspace_id": workspace_id, "run_id": None})()


class _Run:
    def __init__(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self.id = run_id
        self.workspace_id = workspace_id
        self.product_id = None
        self.payload: dict[str, Any] = {}


class _Registry:
    def export_state(self) -> dict[str, Any]:
        return {"declared_contract": {"checks": [{"kind": "shell", "command": "pytest -q"}]}}


async def test_persisting_tool_state_commits() -> None:
    """The latch has to outlive the request, or the agent declares its contract forever."""
    from backend.mcp.tools.work_registry import persist_tool_state

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    session = _Session(_Run(run_id, ws))

    await persist_tool_state(run_id, _Ctx(session, ws), _Registry())  # type: ignore[arg-type]

    assert session.committed, "a flush-only write is rolled back when the MCP request ends"
    assert session.locked, (
        "the read-modify-write must be ROW-LOCKED: the CLI issues tool calls in parallel, and "
        "two unlocked calls erase each other's state (live: run 3e163fc5 lost both its declared "
        "contract and its writes, so the loop nudged and re-dispatched forever)"
    )


async def test_the_state_actually_lands_on_the_run() -> None:
    from backend.mcp.tools.work_registry import WORK_TOOL_STATE_KEY, persist_tool_state

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    run = _Run(run_id, ws)
    session = _Session(run)

    await persist_tool_state(run_id, _Ctx(session, ws), _Registry())  # type: ignore[arg-type]

    assert run.payload[WORK_TOOL_STATE_KEY]["declared_contract"] is not None


async def test_asking_the_founder_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the commit the Decision disappears and the founder is never asked — while the
    tool cheerfully reports success to the agent."""
    from backend.workflow.application import mcp_work_effects

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    session = _Session(_Run(run_id, ws))

    async def _load_run(_rid: Any, _ctx: Any) -> Any:
        return session._run

    async def _create_decision(*_a: Any, **_k: Any) -> Any:
        return type("D", (), {"id": uuid.uuid4()})()

    monkeypatch.setattr(mcp_work_effects, "load_run", _load_run)
    monkeypatch.setattr(mcp_work_effects, "create_decision", _create_decision)

    await mcp_work_effects.record_question(run_id, _Ctx(session, ws), {"question": "?"})  # type: ignore[arg-type]

    assert session.committed


async def test_emitting_a_deliverable_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.workflow.application import mcp_work_effects

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    session = _Session(_Run(run_id, ws))

    async def _load_run(_rid: Any, _ctx: Any) -> Any:
        return session._run

    async def _handle(*_a: Any, **_k: Any) -> str:
        return "recorded"

    monkeypatch.setattr(mcp_work_effects, "load_run", _load_run)
    monkeypatch.setattr(mcp_work_effects, "handle_emit_deliverable", _handle)

    out = await mcp_work_effects.record_deliverable(
        run_id,
        _Ctx(session, ws),  # type: ignore[arg-type]
        {"artifact_type": "code", "summary": "x"},
    )

    assert out == "recorded"
    assert session.committed
