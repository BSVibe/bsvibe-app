"""Unit test for the shared verified-deliverable write helper.

Lift 5b extracts the verified terminal's artifact writes from
``RunOrchestrator._finish_verified`` into ONE source of truth so the native
loop and the new ExecutorOrchestrator land an identical Deliverable contract.
This test pins that contract directly (no orchestrator, no loop): given a run,
the helper writes a CODE Deliverable + a DeliveryEventRow + a settle activity.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from backend.config import get_settings
from backend.storage.product_workspace import (
    add_run_worktree,
    commit_worktree,
    init_product_workspace,
    merge_main_into_worktree,
)
from backend.workflow.domain.verified_deliverable import write_verified_deliverable
from backend.workflow.infrastructure.db import (
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _seed_run(s, *, intent: str, product_id: uuid.UUID | None = None) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=product_id,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    s.add(run)
    await s.flush()
    return run


async def test_write_verified_deliverable_emits_full_contract() -> None:
    async with memory_session() as s:
        run = await _seed_run(s, intent="build it")
        attempt_id = uuid.uuid4()

        deliverable = await write_verified_deliverable(
            s,
            run,
            attempt_id=attempt_id,
            artifact_refs=["src/foo.py"],
            summary="all green",
        )

        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload == {"artifact_refs": ["src/foo.py"], "summary": "all green"}

        event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert event.deliverable_id == deliverable.id
        assert event.artifact_type == DeliverableType.CODE.value
        assert event.payload == {"artifact_refs": ["src/foo.py"], "summary": "all green"}

        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(settle) == 1
        assert settle[0].payload["verified"] is True
        assert settle[0].payload["artifact_refs"] == ["src/foo.py"]
        assert settle[0].payload["summary"] == "all green"
        assert settle[0].payload["intent_text"] == "build it"
        assert settle[0].payload["attempt_id"] == str(attempt_id)
        # No knowledge declared → the settle payload carries no agent_knowledge.
        assert "agent_knowledge" not in settle[0].payload


async def test_write_verified_deliverable_threads_agent_knowledge() -> None:
    """v2 — when the working agent declared knowledge, it rides the settle payload
    as ``agent_knowledge`` {topic, insight} so the sink writes a topic-titled note.
    Routine work declares none and the key is absent (see the test above)."""
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

    async with memory_session() as s:
        run = await _seed_run(s, intent="harden webhooks")
        await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["src/webhooks.py"],
            summary="all green",
            knowledge=RememberableKnowledge(
                topic="Idempotent webhooks",
                insight="Dedupe webhook deliveries by event id — providers retry.",
            ),
        )
        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .one()
        )
        assert settle.payload["agent_knowledge"] == {
            "topic": "Idempotent webhooks",
            "insight": "Dedupe webhook deliveries by event id — providers retry.",
        }


async def test_write_verified_deliverable_truncates_summary_in_event() -> None:
    async with memory_session() as s:
        run = await _seed_run(s, intent="x")
        long_summary = "z" * 900
        await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=[],
            summary=long_summary,
        )
        event = (await s.execute(select(DeliveryEventRow))).scalar_one()
        assert len(event.payload["summary"]) == 500
        settle = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.activity_type == "settle"
                    )
                )
            )
            .scalars()
            .one()
        )
        assert len(settle.payload["summary"]) == 500


@pytest.fixture
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def test_write_verified_deliverable_captures_run_diff_for_product_run(
    _isolate_workspace_roots,
) -> None:
    """Lift 2a: a product run's verified deliverable carries the real
    ``git diff main...HEAD`` in its payload, captured while the run worktree is
    still alive (verify-time, before auto-ship cleanup)."""
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)
    (worktree / "src" / "foo.py").parent.mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "foo.py").write_text("def foo():\n    return 1\n")
    await commit_worktree(product_id, run_id, message="agent: foo()")
    await merge_main_into_worktree(product_id, run_id)

    async with memory_session() as s:
        run = ExecutionRun(
            id=run_id,
            workspace_id=uuid.uuid4(),
            product_id=product_id,
            request_id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            payload={"intent_text": "build foo"},
        )
        s.add(run)
        await s.flush()

        deliverable = await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["src/foo.py"],
            summary="added foo",
        )

        assert "diff" in deliverable.payload
        assert "+def foo():" in deliverable.payload["diff"]
        assert deliverable.payload.get("diff_truncated") is not True
        # The existing keys are untouched.
        assert deliverable.payload["artifact_refs"] == ["src/foo.py"]
        assert deliverable.payload["summary"] == "added foo"


async def test_write_verified_deliverable_omits_diff_for_non_product_run() -> None:
    """A non-product (Direct) run has no worktree / no 'before' state, so the
    payload carries NO diff key — the front end falls back to additions."""
    async with memory_session() as s:
        run = await _seed_run(s, intent="answer it", product_id=None)
        deliverable = await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["note.md"],
            summary="done",
        )
        assert "diff" not in deliverable.payload
        assert deliverable.payload == {"artifact_refs": ["note.md"], "summary": "done"}
