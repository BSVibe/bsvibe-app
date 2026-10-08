"""#673 / #1078 — a deliverable can carry a command's output byte-for-byte.

BStockReport's weekly report: run ``uv run bstockreport run --emit``, publish the
number block it prints between ``<<REPORT_VERBATIM>>`` and ``<<END>>``, add a
short commentary. The agent authored the published text itself
(``emit_deliverable(summary=…)``) and was told by prompt not to touch the block.
Measured 2026-09-14 · 09-21: the marker lines leaked into the Telegram message
twice, and run ``2928e493`` published the report twice. A model in the middle of
a number pipe can also round, restate or — when the tool fails — invent.

``emit_deliverable(verbatim_command=…)`` takes the model out of that pipe: the
SERVER runs the command in the run's own box, puts the block (between the
optional markers, exclusive) at the top of the deliverable exactly as printed,
and appends the agent's ``summary`` as commentary. A failing command, or
markers that are not there, publish NOTHING and say why — never a guess.

And a run that emits the same content twice (no ``external_ref`` to dedupe on)
delivers it once.
"""

from __future__ import annotations

import inspect
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from backend.workflow.domain.emit_deliverable import handle_emit_deliverable
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult
from tests._support import memory_session

pytestmark = pytest.mark.asyncio

_STDOUT = (
    "fetching positions…\n"
    "<<REPORT_VERBATIM>>\n"
    "체결률 61.3% · 승률 54.0% · 주간 PnL +1,284.50 USD\n"
    "  MDD -3.21%\n"
    "<<END>>\n"
    "done\n"
)
_BLOCK = "체결률 61.3% · 승률 54.0% · 주간 PnL +1,284.50 USD\n  MDD -3.21%"


class _Box:
    def __init__(self, stdout: str = _STDOUT, exit_code: int = 0, stderr: str = "") -> None:
        self.stdout, self.exit_code, self.stderr = stdout, exit_code, stderr
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False, **_: Any):
        self.commands.append(command)
        return SandboxResult(
            exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr, timed_out=False
        )


async def _run(session) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(), workspace_id=uuid.uuid4(), status=RunStatus.RUNNING, payload={}
    )
    session.add(run)
    await session.flush()
    return run


async def _deliverables(session) -> list[Deliverable]:
    return list((await session.execute(select(Deliverable))).scalars().all())


def _verbatim(**over: Any) -> dict[str, Any]:
    args = {
        "artifact_type": "direct_output",
        "summary": "이번 주는 승률이 소폭 올랐지만 표본이 작아요.",
        "verbatim_command": "uv run bstockreport run --emit",
        "verbatim_start": "<<REPORT_VERBATIM>>",
        "verbatim_end": "<<END>>",
    }
    args.update(over)
    return args


# ---------------------------------------------------------------------------
# The block is the command's, the commentary is the agent's
# ---------------------------------------------------------------------------


async def test_the_block_is_published_exactly_as_the_command_printed_it() -> None:
    async with memory_session() as session:
        run = await _run(session)
        box = _Box()

        out = json.loads(await handle_emit_deliverable(session, run, _verbatim(), box=box))

        assert out["status"] != "error", out
        (d,) = await _deliverables(session)
        assert box.commands == ["uv run bstockreport run --emit"]
        assert d.payload["summary"].startswith(_BLOCK)
        assert "<<REPORT_VERBATIM>>" not in d.payload["summary"]
        assert "<<END>>" not in d.payload["summary"]
        assert d.payload["summary"].endswith("이번 주는 승률이 소폭 올랐지만 표본이 작아요.")


async def test_without_markers_the_whole_output_is_the_block() -> None:
    async with memory_session() as session:
        run = await _run(session)

        await handle_emit_deliverable(
            session,
            run,
            _verbatim(verbatim_start=None, verbatim_end=None, summary="해설"),
            box=_Box(stdout="a 1\nb 2\n"),
        )

        (d,) = await _deliverables(session)
        assert d.payload["summary"] == "a 1\nb 2\n\n해설"


# ---------------------------------------------------------------------------
# Nothing is published on a doubt
# ---------------------------------------------------------------------------


async def test_a_failing_command_publishes_nothing() -> None:
    async with memory_session() as session:
        run = await _run(session)

        out = json.loads(
            await handle_emit_deliverable(
                session, run, _verbatim(), box=_Box(exit_code=2, stderr="alpaca: 401")
            )
        )

        assert out["status"] == "error"
        assert "alpaca: 401" in out["error"]
        assert await _deliverables(session) == []


async def test_missing_markers_publish_nothing() -> None:
    async with memory_session() as session:
        run = await _run(session)

        out = json.loads(
            await handle_emit_deliverable(
                session, run, _verbatim(), box=_Box(stdout="no block here\n")
            )
        )

        assert out["status"] == "error"
        assert await _deliverables(session) == []


async def test_no_box_publishes_nothing() -> None:
    async with memory_session() as session:
        run = await _run(session)

        out = json.loads(await handle_emit_deliverable(session, run, _verbatim(), box=None))

        assert out["status"] == "error"
        assert await _deliverables(session) == []


# ---------------------------------------------------------------------------
# #1078 — the same content, twice, is delivered once
# ---------------------------------------------------------------------------


async def test_the_same_content_emitted_twice_is_delivered_once() -> None:
    async with memory_session() as session:
        run = await _run(session)
        args = {"artifact_type": "direct_output", "summary": "주간 리포트 본문"}

        await handle_emit_deliverable(session, run, dict(args))
        await handle_emit_deliverable(session, run, dict(args))

        assert len(await _deliverables(session)) == 1


async def test_different_content_is_still_delivered() -> None:
    """Control."""
    async with memory_session() as session:
        run = await _run(session)

        await handle_emit_deliverable(session, run, {"artifact_type": "x", "summary": "하나"})
        await handle_emit_deliverable(session, run, {"artifact_type": "x", "summary": "둘"})

        assert len(await _deliverables(session)) == 2


# ---------------------------------------------------------------------------
# Both transports hand the run's box over
# ---------------------------------------------------------------------------


async def test_the_mcp_transport_hands_the_runs_box_to_a_verbatim_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.workflow.application import mcp_work_effects as fx

    box = _Box()
    seen: dict[str, Any] = {}

    async def _load_run(_run_id: Any, _ctx: Any) -> Any:
        return type("R", (), {"id": uuid.uuid4()})()

    async def _box_for(_run: Any, _ctx: Any) -> Any:
        return box

    async def _emit(_session: Any, _run: Any, arguments: Any, **kw: Any) -> str:
        seen.update(kw)
        return "{}"

    class _Session:
        async def commit(self) -> None:
            return None

    ctx = type("Ctx", (), {"session": _Session()})()
    monkeypatch.setattr(fx, "load_run", _load_run)
    monkeypatch.setattr(fx, "_box_for_run", _box_for)
    monkeypatch.setattr(fx, "handle_emit_deliverable", _emit)

    await fx.record_deliverable(uuid.uuid4(), ctx, _verbatim())

    assert seen.get("box") is box


def test_the_in_process_loop_hands_its_box_to_emit() -> None:
    from backend.workflow.application import _drive_loop

    source = inspect.getsource(_drive_loop)
    call = source[source.index("output = await handle_emit_deliverable(") :][:400]
    assert "box=box" in call
