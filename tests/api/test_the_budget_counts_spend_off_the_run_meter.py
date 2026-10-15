"""The workspace budget counts every token the workspace spent — not only the run meter.

``sum_workspace_tokens_since`` summed ``execution_runs.usage_*`` and nothing else. Two kinds
of spend never reach that meter, by design:

* **run-less calls** (#954) — knowledge ingest, product bootstrap, settle extraction, rule
  compile. They land in ``unattributed_llm_usage``, the ledger built so "#928's billing reads
  it alongside ``execution_runs``". Nothing read it: a workspace could ingest without limit
  and never touch its budget.
* **late executor reports** (#928, #1158) — a turn the backend gave up on (timeout, run
  cancelled mid-turn) reports what it spent AFTER its run moved on. ``record_result`` keeps the
  number on the task row only: folding it into the run would race the drive loop's absolute
  ORM write. So the budget never saw it either.

The fix: a late report ALSO writes a ledger row in the same transaction, and the budget sums
runs + ledger. These go through the real HTTP surfaces — the worker's ``/result`` and the
founder's ``/messages`` door — because the proposition is "spend reported over there is
refused over here", and each half passing alone proves nothing about the other.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.executors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1 import workers as workers_router
from backend.data.rls import cross_tenant_read, workspace_session_scope
from backend.executors import dispatch, service
from backend.executors.db import WorkerRow
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.application.workspace_token_budget import (
    budget_window_start,
    sum_workspace_tokens_since,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus, UnattributedLlmUsage

from .._support import db_engine, fake_current_user, publishing_workspace
from .test_v1_workers_dispatch import _make_redis

pytestmark = pytest.mark.asyncio

_BUDGET = 10_000


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Any]:
    client = await _make_redis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession], redis: Any, workspace_id: uuid.UUID
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_current_user_row] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[workers_router.get_poll_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _inside() -> datetime:
    return budget_window_start() + timedelta(seconds=1)


async def _seed_workspace(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, *, run_tokens: int
) -> uuid.UUID:
    """A workspace on ``_BUDGET`` with one run that spent ``run_tokens``. Returns the run id."""
    now = datetime.now(tz=UTC)
    product_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, workspace_id):
        s.add(WorkspaceRow(id=workspace_id, name="w", created_at=now, updated_at=now))
        await s.flush()
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == workspace_id)
            .values(monthly_token_budget=_BUDGET)
        )
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                created_at=now,
                updated_at=now,
            )
        )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                product_id=product_id,
                status=RunStatus.CANCELLED,
                payload={},
                usage_prompt_tokens=run_tokens,
                usage_completion_tokens=0,
                created_at=_inside(),
                updated_at=_inside(),
            )
        )
        await s.commit()
    return run_id


async def _seed_ledger(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, tokens: int, at: datetime
) -> None:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        s.add(
            UnattributedLlmUsage(
                workspace_id=workspace_id,
                site="knowledge_ingest",
                identity={},
                usage_prompt_tokens=tokens - tokens // 4,
                usage_completion_tokens=tokens // 4,
                created_at=at,
            )
        )
        await s.commit()


async def _timed_out_task(
    sf: async_sessionmaker[AsyncSession], redis: Any, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[uuid.UUID, str]:
    """A task the backend already closed ``failed`` (its awaiter timed out) — the row a late
    report lands on. Returns ``(task_id, worker_token)``."""
    token = uuid.uuid4().hex
    async with sf() as s, workspace_session_scope(s, workspace_id):
        worker = WorkerRow(
            workspace_id=workspace_id,
            name="w",
            labels=[],
            capabilities=["claude_code"],
            status="online",
            last_heartbeat=datetime.now(UTC),
            token_hash=service._hash_token(token),
            is_active=True,
        )
        s.add(worker)
        await s.flush()
        task = await dispatch.create_task(
            s, workspace_id=workspace_id, executor_type="claude_code", prompt="p", run_id=run_id
        )
        await s.flush()
        await dispatch.dispatch_task(redis, session=s, task=task, worker_id=worker.id)
        task.status = "failed"
        task.error_message = "no worker result arrived within 600s"
        await s.commit()
        return task.id, token


async def _report(
    client: httpx.AsyncClient, task_id: uuid.UUID, token: str, tokens: int
) -> httpx.Response:
    return await client.post(
        "/api/v1/workers/result",
        headers={"X-Worker-Token": token},
        json={
            "task_id": str(task_id),
            "success": False,
            "output": "",
            "error_message": "cancelled by the backend; usage reported for the record",
            "usage_prompt_tokens": tokens - 100,
            "usage_completion_tokens": 100,
        },
    )


async def _ledger_rows(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> list[UnattributedLlmUsage]:
    async with sf() as s, workspace_session_scope(s, workspace_id):
        return list((await s.scalars(select(UnattributedLlmUsage))).all())


# ── Run-less spend ───────────────────────────────────────────────────────────


async def test_run_less_spend_counts_toward_the_budget(sf, client, workspace_id) -> None:
    await _seed_workspace(sf, workspace_id, run_tokens=4_000)
    await _seed_ledger(sf, workspace_id, 7_000, _inside())

    response = await client.post("/api/v1/messages", json={"text": "more"})

    assert response.status_code == 429, response.text


async def test_run_less_spend_before_the_window_does_not_count(sf, client, workspace_id) -> None:
    """Control for the one above: the ledger is windowed like the runs are — otherwise a
    ledger that only ever grows would refuse every workspace eventually."""
    await _seed_workspace(sf, workspace_id, run_tokens=4_000)
    await _seed_ledger(sf, workspace_id, 7_000, budget_window_start() - timedelta(days=1))

    response = await client.post("/api/v1/messages", json={"text": "more"})

    assert response.status_code == 202, response.text


async def test_another_workspaces_ledger_is_not_this_ones_spend(sf, workspace_id) -> None:
    """Read across tenants on purpose: inside a workspace scope the ORM auto-filter (layer 2)
    hides the other tenant's row on its own, and this would pass with the query's own
    ``workspace_id`` predicate deleted (measured). ``cross_tenant_read`` lets every tenant's
    rows through on both tiers (RLS ``'*'`` on Postgres), so the predicate is what is under
    test."""
    await _seed_workspace(sf, workspace_id, run_tokens=4_000)
    await _seed_ledger(sf, uuid.uuid4(), 7_000, _inside())

    with cross_tenant_read():
        async with sf() as s:
            total = await sum_workspace_tokens_since(s, workspace_id, budget_window_start())
    assert total == 4_000


# ── A late executor report ───────────────────────────────────────────────────


async def test_a_late_report_is_refused_at_the_door_it_never_passed(
    sf, redis, client, workspace_id
) -> None:
    """End to end: the worker reports a cancelled session's spend on ``/result``; the
    founder's next submission is measured against it."""
    run_id = await _seed_workspace(sf, workspace_id, run_tokens=4_000)
    task_id, token = await _timed_out_task(sf, redis, workspace_id, run_id)

    assert (await _report(client, task_id, token, 7_000)).status_code == 200
    response = await client.post("/api/v1/messages", json={"text": "more"})

    assert response.status_code == 429, response.text


async def test_a_late_report_lands_in_the_ledger_once_with_its_run(
    sf, redis, client, workspace_id
) -> None:
    run_id = await _seed_workspace(sf, workspace_id, run_tokens=0)
    task_id, token = await _timed_out_task(sf, redis, workspace_id, run_id)

    await _report(client, task_id, token, 7_000)
    # A replay: the row now holds usage, so #928's guard refuses it — and must write no
    # second ledger row, or one session would be billed twice.
    await _report(client, task_id, token, 7_000)

    rows = await _ledger_rows(sf, workspace_id)
    assert len(rows) == 1
    (row,) = rows
    assert (row.usage_prompt_tokens, row.usage_completion_tokens) == (6_900, 100)
    assert row.identity == {"task_id": str(task_id), "run_id": str(run_id)}


async def test_an_on_time_report_writes_no_ledger_row(sf, redis, client, workspace_id) -> None:
    """Control: a report that closes its task is read back by the adapter into the RUN's
    meter. A ledger row as well would count the same tokens twice."""
    run_id = await _seed_workspace(sf, workspace_id, run_tokens=0)
    task_id, token = await _timed_out_task(sf, redis, workspace_id, run_id)
    async with sf() as s, workspace_session_scope(s, workspace_id):
        from backend.executors.db import ExecutorTaskRow

        task = await s.get(ExecutorTaskRow, task_id)
        assert task is not None
        task.status = "dispatched"
        task.error_message = None
        await s.commit()

    assert (await _report(client, task_id, token, 7_000)).status_code == 200

    assert await _ledger_rows(sf, workspace_id) == []
