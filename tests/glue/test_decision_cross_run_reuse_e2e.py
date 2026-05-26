"""B11b — Cross-run reuse: a resolved decision informs a future run.

The lift's load-bearing assertion: a decision answered today must show up in a
NEW run's verify contract / B6 seed tomorrow, so the agent doesn't re-ask.

Pipeline this test exercises (one workspace, two phases):

1. Phase A — resolve a paused decision via the API. The settle activity lands
   on ``execution_run_activities``.
2. Drain the settle worker against the SAME workspace's vault root.
3. Phase B — build a FRESH :class:`KnowledgeFactory` for the same workspace and
   region. Its retriever (the SAME seam the verifier + B6 seed inject) must
   surface the prior decision for an overlapping signal.

No native agent loop is spun up — the contract being asserted is that the
retriever, which IS the seam B3/B6 inject, returns the resolved decision. The
glue test for the loop itself stays at ``test_decision_resolve_e2e.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.execution.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from backend.knowledge.factory import KnowledgeFactory
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


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


async def test_second_run_retriever_sees_prior_resolved_decision(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],  # type: ignore[name-defined]  # noqa: F821
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Phase A: resolve + drain → settle vault note. Phase B: NEW factory's
    retriever surfaces the resolved decision for an overlapping signal."""
    # Phase A — seed paused run with a pending Decision.
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "pick the production database"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "Which database should I target?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    # Resolve the decision via the API surface.
    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres"},
    )
    assert r.status_code == 200, r.text

    # Drain the settle worker so the decision lands as a vault note. Same
    # vault_root passed to the second-run KnowledgeFactory.
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    assert await worker.drain_once() == 1

    # Phase B — fresh KnowledgeFactory for the SAME workspace (mirrors the
    # production wiring at ``workers/run.py::_retriever_for``).
    factory = KnowledgeFactory(
        region="us-1",
        workspace_id=str(workspace_id),
        vault_root=tmp_path,
    )
    retriever = factory.retriever()
    statements = await retriever.retrieve_for_signals(
        "Pick a database for the new analytics service"
    )
    # The future run's signals overlap the prior decision's topic; the
    # retriever (the same seam B3 verifier + B6 seed inject) must surface it.
    assert statements, "expected at least one resolved-decision statement"
    joined = "\n".join(statements)
    assert "Postgres" in joined, statements


async def test_second_run_workspace_isolation(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],  # type: ignore[name-defined]  # noqa: F821
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """A decision resolved in workspace A never leaks to workspace B's retriever."""
    # Phase A — resolve in workspace_id (the test's caller workspace).
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "pick the production database"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "Which database should I target?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres"},
    )
    assert r.status_code == 200, r.text

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    assert await worker.drain_once() == 1

    # Phase B — DIFFERENT workspace. Its retriever sees no leaked decisions.
    other_workspace = uuid.uuid4()
    factory = KnowledgeFactory(
        region="us-1",
        workspace_id=str(other_workspace),
        vault_root=tmp_path,
    )
    statements = await factory.retriever().retrieve_for_signals(
        "Pick a database for the new analytics service"
    )
    assert statements == []
