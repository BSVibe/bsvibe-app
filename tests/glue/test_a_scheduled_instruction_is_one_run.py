"""#1079 — a scheduled instruction is worked in ONE run.

Measured 2026-09-29: the BStockReport weekly report — "run one command, publish
its output" — was framed into a two-step plan. Step 1 (``cc68f583``) ran the
shell 13 times, passed its gate, delivered nothing and parked at
``review_ready``; step 2 (``a65412ad``) started from scratch on the same
directive (``has_prior_output=false``) and published. Twice the tokens, and two
``review_ready`` runs holding the concurrent-run cap until someone discarded the
first by hand.

A schedule's instruction is a unit the founder authored once and fires
unchanged on every tick; there is nothing in it a later step could need an
earlier step's output for. So framing a SCHEDULE-sourced request is never asked
to split: it gets no stage vocabulary (the same "no vocabulary → no split" rule
a workspace without stage rules already follows).
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.router.routing.run_routing.chaining import StageTerm
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.workers import agent_worker as aw

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_VOCAB = [StageTerm(label="design"), StageTerm(label="implement")]


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _the_workspace_has_stage_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _vocab(_session: AsyncSession, _workspace_id: uuid.UUID) -> list[StageTerm]:
        return list(_VOCAB)

    monkeypatch.setattr(aw, "_stage_vocabulary_for", _vocab)


async def _request(sf_, kind: TriggerKind) -> tuple[uuid.UUID, RequestRow]:
    ws = uuid.uuid4()
    async with sf_() as s, workspace_session_scope(s, ws):
        trig = TriggerEventRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            source="schedule" if kind is TriggerKind.SCHEDULE else "direct",
            trigger_kind=kind,
            idempotency_key=uuid.uuid4().hex,
            payload={"intent_text": "uv run bstockreport run --emit 의 결과를 발행해"},
            received_at=datetime.now(tz=UTC),
        )
        s.add(trig)
        await s.flush()
        req = RequestRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            trigger_event_id=trig.id,
            status=RequestStatus.OPEN,
            payload={},
        )
        s.add(req)
        await s.commit()
    return ws, req


async def test_a_scheduled_request_is_given_no_stages_to_split_into(sf) -> None:
    ws, req = await _request(sf, TriggerKind.SCHEDULE)

    async with sf() as s, workspace_session_scope(s, ws):
        assert await aw._split_vocabulary_for(s, req) == []


@pytest.mark.parametrize("kind", [TriggerKind.DIRECT, TriggerKind.WEBHOOK])
async def test_other_requests_keep_the_founders_stages(sf, kind: TriggerKind) -> None:
    """Control — only the schedule's fixed instruction is exempt."""
    ws, req = await _request(sf, kind)

    async with sf() as s, workspace_session_scope(s, ws):
        assert await aw._split_vocabulary_for(s, req) == _VOCAB


def test_framing_asks_for_the_split_vocabulary_of_the_request() -> None:
    """The wire: the frame call site uses the per-request answer, not the
    workspace-wide vocabulary."""
    source = inspect.getsource(aw.AgentWorker._frame_and_drive)

    assert "_split_vocabulary_for(session, request)" in source
    assert "_stage_vocabulary_for(" not in source
