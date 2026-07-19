"""Producer-existence proof + dedupe for the ``needs_you`` outbox (Notifier N2).

The [P] test is the anti-"unwired stub" gate the handoff §5 demands. This
defect class — a producer that exists in design but is never called in
production — passes every unit test that mocks the producer. So [P] drives the
REAL production effect chain (``record_question`` → ``create_decision`` →
``NOTIFICATION_OUTBOX.emit``) against a real DB session, with NO
``dependency_overrides`` and NO pre-seeded outbox row, and asserts a real
:class:`NotificationEventRow` actually lands. If the emit is ever un-wired from
``create_decision``, this fails loudly.

[D] pins the dedupe: the UNIQUE ``dedupe_key`` makes a re-emit of the same
Decision's notification a DB-level no-op, so the founder is called exactly once
per Decision even under a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select

# Register the tables these tests touch on the shared Base.metadata.
import backend.notifications.db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.mcp.api import McpPrincipal, ToolContext
from backend.notifications.db import NotificationEventRow, NotificationStatus
from backend.workflow.application import mcp_work_effects
from backend.workflow.application.run_persistence import _emit_needs_you, create_decision
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import memory_session

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncIterator:
    async with memory_session() as s:
        yield s


def _principal(workspace_id: uuid.UUID, run_id: uuid.UUID) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        client_id="bsvibe-worker",
        scopes=frozenset({"mcp:read", "mcp:write"}),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


async def _make_run(session, workspace_id: uuid.UUID) -> ExecutionRun:
    run = ExecutionRun(workspace_id=workspace_id, status=RunStatus.RUNNING, payload={})
    session.add(run)
    await session.commit()
    return run


async def test_asking_the_founder_queues_a_needs_you_notification(session) -> None:
    """[P] the real MCP ask_user_question effect emits a real outbox row.

    No mock of ``create_decision`` / the emit, no ``dependency_overrides``, no
    pre-seeded notification — just a run + the production ``record_question``.
    """
    ws = uuid.uuid4()
    run = await _make_run(session, ws)
    ctx = ToolContext(principal=_principal(ws, run.id), session=session)

    decision_id = await mcp_work_effects.record_question(
        run.id, ctx, {"question": "Postgres or SQLite?"}
    )

    rows = (await session.execute(select(NotificationEventRow))).scalars().all()
    assert len(rows) == 1, "the founder was never queued — the producer is unwired"
    row = rows[0]
    assert row.event == "needs_you"
    assert row.dedupe_key == f"needs_you:{decision_id}"
    assert row.workspace_id == ws
    assert row.status is NotificationStatus.PENDING
    assert row.payload["decision_id"] == decision_id
    assert row.payload["run_id"] == str(run.id)
    assert "Postgres or SQLite?" in row.payload["body"]
    assert row.payload["link"] == "/decisions"


async def test_every_decision_kind_queues_needs_you(session) -> None:
    """create_decision is the SOLE live path — any Decision kind calls the founder."""
    ws = uuid.uuid4()
    run = await _make_run(session, ws)

    decision = await create_decision(
        session,
        run,
        None,
        kind="no_executor_dispatch_transport",
        payload={},
        rationale="the run cannot dispatch — a decision is needed",
    )
    await session.commit()

    row = (
        await session.execute(
            select(NotificationEventRow).where(
                NotificationEventRow.dedupe_key == f"needs_you:{decision.id}"
            )
        )
    ).scalar_one()
    assert row.event == "needs_you"
    # No question in the payload → the body falls back to the Decision rationale.
    assert "cannot dispatch" in row.payload["body"]


async def test_re_emitting_the_same_decision_is_deduped_to_one_row(session) -> None:
    """[D] a retried emit of the same Decision's notification is a DB-level no-op."""
    ws = uuid.uuid4()
    run = await _make_run(session, ws)
    decision = await create_decision(
        session, run, None, kind="ask_user_question", payload={"question": "?"}, rationale="r"
    )
    await session.commit()

    # A retry re-emits the SAME decision. The UNIQUE dedupe_key + savepoint make
    # it a no-op — the founder is not double-notified.
    await _emit_needs_you(session, run, decision)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(NotificationEventRow).where(
                    NotificationEventRow.dedupe_key == f"needs_you:{decision.id}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_the_decision_survives_a_duplicate_notification_emit(session) -> None:
    """The savepoint isolates the dup: only the outbox insert rolls back, the
    Decision (flushed before the savepoint) is intact."""
    from backend.workflow.infrastructure.db import Decision

    ws = uuid.uuid4()
    run = await _make_run(session, ws)
    decision = await create_decision(
        session, run, None, kind="ask_user_question", payload={"question": "?"}, rationale="r"
    )
    await _emit_needs_you(session, run, decision)  # duplicate within the same txn
    await session.commit()

    persisted = await session.get(Decision, decision.id)
    assert persisted is not None
