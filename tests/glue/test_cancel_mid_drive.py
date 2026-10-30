"""A run cancelled / discarded while its drive loop is live must stop and write nothing.

Prod 2026-10-29 (#1263, run ``14e222a1``): ``bsvibe_runs_discard`` returned ``cancelled``, yet the
drive that was already running went on to verify and created a Deliverable + Safe Mode item for the
cancelled run. These tests run the real ``RunOrchestrator.run`` and flip the status from a SEPARATE
session (the way a founder action / operator reaches a live drive).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.workflow.infrastructure.delivery.db  # noqa: F401 — registers tables on Base.metadata
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.sandbox import NoopSandboxManager

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


class _HookedLlm:
    """Scripted ``LoopLlm`` that runs an async hook right before returning a given turn."""

    def __init__(self, turns: list[LoopTurn], hooks: dict[int, Any] | None = None) -> None:
        self._turns = list(turns)
        self._hooks = hooks or {}
        self.calls = 0

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        index = self.calls
        self.calls += 1
        if not self._turns:
            raise AssertionError("loop requested an unscripted turn")
        if index in self._hooks:
            await self._hooks[index]()
        return self._turns.pop(0)


def _turns() -> list[LoopTurn]:
    return [
        LoopTurn(
            content="Writing the deliverable and declaring how to check it.",
            tool_calls=(
                _tc(
                    "declare_verification",
                    checks=[{"kind": "command", "command": "test -f answer.txt"}],
                ),
                _tc("file_write", path="answer.txt", content="42\n"),
            ),
        ),
        LoopTurn(content="Done — answer.txt written.", tool_calls=()),
    ]


async def _seed_run(sf: async_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    ws_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws_id):
        s.add(
            WorkspaceRow(
                id=ws_id,
                name="test-ws",
                safe_mode=True,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "write the answer"},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return ws_id, run_id


async def _cancel_from_other_session(
    sf: async_sessionmaker, ws_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    async with sf() as other, workspace_session_scope(other, ws_id):
        await other.execute(
            update(ExecutionRun).where(ExecutionRun.id == run_id).values(status=RunStatus.CANCELLED)
        )
        await other.commit()


async def _drive(
    sf: async_sessionmaker, ws_id: uuid.UUID, run_id: uuid.UUID, llm: _HookedLlm, tmp_path: Path
) -> Any:
    async with sf() as s, workspace_session_scope(s, ws_id):
        run = await s.get(ExecutionRun, run_id)
        orch = RunOrchestrator(session=s, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        await s.commit()
    return result


async def _artifact_counts(
    sf: async_sessionmaker, ws_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[int, int]:
    async with sf() as s, workspace_session_scope(s, ws_id):
        deliverables = await s.scalar(
            select(func.count()).select_from(Deliverable).where(Deliverable.run_id == run_id)
        )
        events = await s.scalar(
            select(func.count())
            .select_from(DeliveryEventRow)
            .where(DeliveryEventRow.run_id == run_id)
        )
    return int(deliverables or 0), int(events or 0)


async def test_cancel_during_final_turn_stops_before_verify(sf, tmp_path: Path) -> None:
    ws_id, run_id = await _seed_run(sf)

    async def cancel() -> None:
        await _cancel_from_other_session(sf, ws_id, run_id)

    llm = _HookedLlm(_turns(), hooks={1: cancel})
    result = await _drive(sf, ws_id, run_id, llm, tmp_path)

    assert result.outcome != "verified"
    assert result.verification_result_id is None
    assert await _artifact_counts(sf, ws_id, run_id) == (0, 0)
    async with sf() as s, workspace_session_scope(s, ws_id):
        assert await s.scalar(select(ExecutionRun.status).where(ExecutionRun.id == run_id)) is (
            RunStatus.CANCELLED
        )


async def test_cancel_during_verify_writes_no_artifacts(sf, tmp_path: Path, monkeypatch) -> None:
    ws_id, run_id = await _seed_run(sf)
    from backend.workflow.application.verification_service import VerificationService

    real_verify = VerificationService.verify

    async def verify_then_cancel(self, **kwargs: Any) -> Any:
        verdict = await real_verify(self, **kwargs)
        await _cancel_from_other_session(sf, ws_id, run_id)
        return verdict

    monkeypatch.setattr(VerificationService, "verify", verify_then_cancel)

    result = await _drive(sf, ws_id, run_id, _HookedLlm(_turns()), tmp_path)

    assert result.outcome != "verified"
    assert await _artifact_counts(sf, ws_id, run_id) == (0, 0)


async def test_artifact_write_point_refuses_a_cancelled_run(sf, tmp_path: Path) -> None:
    """Even a caller that skipped every checkpoint cannot create artifacts for a cancelled run."""
    from backend.workflow.domain.verified_deliverable import (
        RunCancelledError,
        write_verified_deliverable,
    )

    ws_id, run_id = await _seed_run(sf)
    await _cancel_from_other_session(sf, ws_id, run_id)
    async with sf() as s, workspace_session_scope(s, ws_id):
        stale_run = ExecutionRun(
            id=run_id, workspace_id=ws_id, status=RunStatus.RUNNING, payload={}
        )  # the drive's in-memory copy still says RUNNING
        with pytest.raises(RunCancelledError):
            await write_verified_deliverable(
                s, stale_run, attempt_id=uuid.uuid4(), artifact_refs=["a.txt"], summary="x"
            )
    assert await _artifact_counts(sf, ws_id, run_id) == (0, 0)


async def test_normal_run_still_creates_deliverable_and_card(sf, tmp_path: Path) -> None:
    ws_id, run_id = await _seed_run(sf)

    result = await _drive(sf, ws_id, run_id, _HookedLlm(_turns()), tmp_path)

    assert result.outcome == "verified"
    assert await _artifact_counts(sf, ws_id, run_id) == (1, 1)
