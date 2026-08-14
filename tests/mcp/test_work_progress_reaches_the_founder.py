"""The row is not the point — the founder seeing it is.

``record_progress`` writing an activity row proves nothing on its own: the loop
already wrote rows the timeline deliberately DROPS as noise. What matters is
whether an executor's work now appears on the run's story while it is still
working. So this drives the real effect against a real session and then feeds the
row it produced to the real timeline builder.

(Asserting the piece and not the assembled result is how #748 and #750 reached
production with green unit tests.)
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from backend.api.v1.runs._helpers import _activity_label
from backend.mcp.api import McpPrincipal, ToolContext
from backend.workflow.application.mcp_work_effects import record_progress
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


async def _seed(session: Any) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": "do the thing"},
    )
    session.add(run)
    await session.flush()
    return run


def _ctx(session: Any, workspace_id: uuid.UUID, run_id: uuid.UUID) -> ToolContext:
    return ToolContext(
        principal=McpPrincipal(
            user_id=uuid.uuid4(),
            workspace_id=workspace_id,
            client_id="bsvibe-worker",
            scopes=frozenset({"mcp:read", "mcp:write"}),
            jti=uuid.uuid4(),
            run_id=run_id,
        ),
        session=session,
    )


async def test_a_write_lands_as_an_activity_row_on_the_run() -> None:
    async with memory_session() as session:
        run = await _seed(session)

        await record_progress(
            run.id,
            _ctx(session, run.workspace_id, run.id),
            {"tool": "file_write", "ok": True, "writes": ["backend/new.py"]},
        )

        rows = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert len(rows) == 1
        assert rows[0].run_id == run.id
        assert rows[0].workspace_id == run.workspace_id
        assert rows[0].activity_type == "tool_call"
        assert rows[0].payload["writes"] == ["backend/new.py"]
        # The transport acts outside any loop attempt — recorded honestly as absent.
        assert rows[0].payload["attempt_id"] is None


async def test_the_founder_timeline_says_delivered() -> None:
    """The consumer's view, not the producer's. Same label the native loop earns."""
    async with memory_session() as session:
        run = await _seed(session)
        await record_progress(
            run.id,
            _ctx(session, run.workspace_id, run.id),
            {"tool": "file_write", "ok": True, "writes": ["backend/new.py"]},
        )
        row = (await session.execute(select(ExecutionRunActivity))).scalars().one()

        assert _activity_label(row.activity_type, row.payload) == "Delivered backend/new.py"


async def test_a_read_only_call_stays_off_the_timeline_but_stays_in_the_table() -> None:
    """Liveness without noise: the row proves the run is moving; the timeline
    still only tells the story."""
    async with memory_session() as session:
        run = await _seed(session)
        await record_progress(
            run.id,
            _ctx(session, run.workspace_id, run.id),
            {"tool": "file_read", "ok": True, "writes": []},
        )
        row = (await session.execute(select(ExecutionRunActivity))).scalars().one()

        assert _activity_label(row.activity_type, row.payload) is None
        assert row.created_at is not None


async def test_another_workspaces_token_cannot_write_progress_onto_this_run() -> None:
    """``record_progress`` goes through the same ``load_run`` guard as every other
    work effect — a new effect must not become a new hole."""
    from backend.mcp.api import ToolError

    async with memory_session() as session:
        run = await _seed(session)
        with pytest.raises(ToolError, match="another workspace"):
            await record_progress(
                run.id,
                _ctx(session, uuid.uuid4(), run.id),
                {"tool": "file_write", "ok": True, "writes": ["x.py"]},
            )
