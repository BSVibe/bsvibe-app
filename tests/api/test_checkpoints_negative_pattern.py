"""G1 — Discarding a deliverable with a reason emits a negative-pattern settle.

When the founder discards an executor B2b Decision and gives a *reason*, that
rejection must become reusable negative knowledge: the resolve endpoint records
a second ``settle``-class :class:`ExecutionRunActivity` carrying
``kind = negative_pattern`` + the reason + stable run context. The
:class:`SettleWorker` drains it into the workspace vault as a
``garden/seedling`` note so a future run with similar signals surfaces the
founder's "don't do this again" guidance.

These tests pin the WRITE contract at the API surface and end-to-end through the
settle worker. The negative-pattern settle is ADDITIVE — the existing
``decision_resolution`` settle (B11b) is untouched; a discard with no reason, or
a non-discard action, emits no negative-pattern row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NEGATIVE_KIND = "negative_pattern"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, founder_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_pending_executor_decision(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    reason: str = "the orders list still 500s on an empty page",
    intent_text: str | None = "add pagination to the orders list",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a paused run + a pending executor B2b Decision (kind
    ``verification_failed`` — carries the ship/discard one-click actions)."""
    async with sf() as s:
        payload: dict[str, object] = {"text": intent_text or ""}
        if intent_text:
            payload["intent_text"] = intent_text
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload=payload,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="verification_failed",
            payload={"reason": reason},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        return run.id, decision.id


async def _settle_rows(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> list[ExecutionRunActivity]:
    async with sf() as s:
        return list(
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.workspace_id == workspace_id,
                        ExecutionRunActivity.activity_type == "settle",
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_discard_with_reason_emits_negative_pattern_settle(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    """Discarding with a reason emits a ``negative_pattern`` settle row carrying
    the reason + stable run context (additive to the decision_resolution row)."""
    _run_id, decision_id = await _seed_pending_executor_decision(
        sf, workspace_id, reason="never merge without the regression test passing"
    )

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"action_key": "discard", "reason": "never merge without the regression test passing"},
    )
    assert r.status_code == 200, r.text

    rows = await _settle_rows(sf, workspace_id)
    negatives = [a for a in rows if a.payload.get("kind") == _NEGATIVE_KIND]
    assert len(negatives) == 1, [a.payload for a in rows]
    payload = negatives[0].payload
    assert payload["reason"] == "never merge without the regression test passing"
    assert payload["decision_id"] == str(decision_id)
    assert payload["resolved_by"] == str(founder_id)
    # Stable run context rides through so the retriever can match by signal.
    assert payload["intent_text"] == "add pagination to the orders list"
    # A rejection is NEVER verified-as-code.
    assert payload["verified"] is False
    assert "never merge" in payload["summary"]


async def test_discard_negative_pattern_drains_into_vault(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """The negative-pattern settle drains into the workspace vault as a
    ``garden/seedling`` note carrying ``kind: negative_pattern`` + the reason."""
    _run_id, decision_id = await _seed_pending_executor_decision(
        sf, workspace_id, intent_text="pick a web framework"
    )

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"action_key": "discard", "reason": "do not use Flask, stick with FastAPI"},
    )
    assert r.status_code == 200, r.text

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    # Drain everything queued (decision_resolution + negative_pattern).
    while await worker.drain_once():
        pass

    ws_dir = tmp_path / "us-1" / str(workspace_id)
    garden_notes = [p for p in ws_dir.rglob("*.md") if "garden/" in p.as_posix()]
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in garden_notes)
    assert "negative_pattern" in bodies
    assert "do not use Flask" in bodies


async def test_discard_without_reason_emits_no_negative_pattern(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    """A discard with a blank/absent reason emits NO negative-pattern row — there
    is nothing to learn from a reasonless rejection."""
    _run_id, decision_id = await _seed_pending_executor_decision(sf, workspace_id)

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"action_key": "discard"},
    )
    assert r.status_code == 200, r.text

    rows = await _settle_rows(sf, workspace_id)
    assert not [a for a in rows if a.payload.get("kind") == _NEGATIVE_KIND], [
        a.payload for a in rows
    ]


async def test_ship_action_emits_no_negative_pattern(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    """Shipping (approving) emits no negative-pattern row even if a reason rides
    along — only a discard is a rejection."""
    _run_id, decision_id = await _seed_pending_executor_decision(sf, workspace_id)

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"action_key": "ship", "reason": "looks good"},
    )
    assert r.status_code == 200, r.text

    rows = await _settle_rows(sf, workspace_id)
    assert not [a for a in rows if a.payload.get("kind") == _NEGATIVE_KIND], [
        a.payload for a in rows
    ]
