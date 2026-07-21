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
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.identity.workspaces_db import WorkspaceRow
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


async def _make_workspace(session, *, language: str) -> uuid.UUID:
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="WS", language=language))
    await session.commit()
    return ws


async def test_needs_you_title_localizes_to_workspace_language(session) -> None:
    """A KO workspace gets a KO ``needs_you`` title; the founder's question stays
    verbatim. An EN workspace gets the English title."""
    ko_ws = await _make_workspace(session, language="ko")
    ko_run = await _make_run(session, ko_ws)
    await create_decision(
        session,
        ko_run,
        None,
        kind="ask_user_question",
        payload={"question": "Postgres 인가요 SQLite 인가요?"},
        rationale="r",
    )
    await session.commit()

    ko_row = (
        await session.execute(
            select(NotificationEventRow).where(NotificationEventRow.workspace_id == ko_ws)
        )
    ).scalar_one()
    assert ko_row.payload["title"] == "결정이 필요한 작업이 있어요"
    # The founder's actual question is preserved verbatim.
    assert ko_row.payload["body"] == "Postgres 인가요 SQLite 인가요?"

    en_ws = await _make_workspace(session, language="en")
    en_run = await _make_run(session, en_ws)
    await create_decision(
        session,
        en_run,
        None,
        kind="ask_user_question",
        payload={"question": "Postgres or SQLite?"},
        rationale="r",
    )
    await session.commit()

    en_row = (
        await session.execute(
            select(NotificationEventRow).where(NotificationEventRow.workspace_id == en_ws)
        )
    ).scalar_one()
    assert en_row.payload["title"] == "A run needs your decision"
    assert en_row.payload["body"] == "Postgres or SQLite?"


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
    # The decisions tab was removed (unified into the Brief) — needs_you deep-links there.
    assert row.payload["link"] == "/brief"


async def test_every_decision_kind_queues_needs_you(session) -> None:
    """create_decision is the SOLE live path — any Decision kind calls the founder.

    NC1: a system-minted Decision with NO ``question`` must NOT leak the English
    ``decision.rationale`` into the founder-facing body — an unknown reason maps
    to the generic localized fallback instead.
    """
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
    # No question → generic localized fallback, NEVER the English rationale.
    assert "cannot dispatch" not in row.payload["body"]
    assert row.payload["body"] == "A run has paused and needs your input."


async def test_verify_gate_needs_you_maps_reason_to_friendly_copy(session) -> None:
    """NC1 — a verify-gate Decision (reason=weak_evidence_no_gate, no question)
    renders the warm localized sentence, not the raw English honesty-grade
    rationale the KO founder saw in prod."""
    ws = await _make_workspace(session, language="ko")
    run = await _make_run(session, ws)
    decision = await create_decision(
        session,
        run,
        None,
        kind="human_review_required",
        payload={"reason": "weak_evidence_no_gate", "honesty_grade": "D"},
        rationale="verified but the target declares no gate to run — weak evidence (grade D)",
    )
    await session.commit()

    row = (
        await session.execute(
            select(NotificationEventRow).where(
                NotificationEventRow.dedupe_key == f"needs_you:{decision.id}"
            )
        )
    ).scalar_one()
    assert row.payload["body"] == "작업을 마쳤지만 검증 근거가 약해요. 확인해주세요."
    for jargon in ("grade", "gate", "weak evidence", "declares no"):
        assert jargon not in row.payload["body"]


async def test_ask_user_question_body_is_the_question_verbatim(session) -> None:
    """NC1 — the ask_user_question path (payload HAS a question) is UNCHANGED:
    the agent's own localized question rides through verbatim."""
    ws = await _make_workspace(session, language="ko")
    run = await _make_run(session, ws)
    decision = await create_decision(
        session,
        run,
        None,
        kind="ask_user_question",
        payload={"question": "어느 리전에 배포할까요?"},
        rationale="ignored English rationale",
    )
    await session.commit()

    row = (
        await session.execute(
            select(NotificationEventRow).where(
                NotificationEventRow.dedupe_key == f"needs_you:{decision.id}"
            )
        )
    ).scalar_one()
    assert row.payload["body"] == "어느 리전에 배포할까요?"


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
