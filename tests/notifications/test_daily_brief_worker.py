"""DailyBriefWorker — the once-a-day founder digest producer (Notifier, daily_brief).

daily_brief is the fifth (deferred) notification moment. Unlike the four
terminal-write producers (needs_you / triggered / shipped / failed), it has no
single triggering write — it is a per-workspace DIGEST emitted once per local
day at the workspace's morning hour. These tests are the anti-"unwired stub"
gate for that producer:

* [P] a workspace that opted in + has shipped/failed runs + a pending decision,
  ticked at its local morning ⇒ exactly one ``daily_brief`` outbox row whose
  body carries the right counts. An off-window tick ⇒ no row. A second tick the
  same local day ⇒ still one row (the local-date dedupe key is a DB no-op).
* A workspace with daily_brief disabled (or never opted in) ⇒ no row.
* Timezone — "morning" is evaluated in the workspace's own IANA zone: an
  Asia/Seoul workspace briefs at KST morning, a UTC workspace does not, for the
  SAME wall-clock instant.

The delivery half (matrix + quiet hours + push fan-out) is the NotifyWorker's
job and is covered in ``test_notify_worker`` — these tests only prove the
producer emits.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register the tables these tests touch on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import (
    NotificationEventRow,
    NotificationPrefsRow,
    NotificationStatus,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.workers.daily_brief_worker import (
    DailyBriefWorker,
    DailyBriefWorkerConfig,
)

from .._support import db_engine

# A fixed instant: 23:30 UTC. In Asia/Seoul (UTC+9) that is the NEXT day 08:30 —
# inside the [08:00, 09:00) morning window. In UTC it is 23:30 — NOT morning.
_KST_MORNING_UTC = datetime(2026, 7, 20, 23, 30, tzinfo=UTC)
# 08:30 UTC — morning for a UTC workspace.
_UTC_MORNING = datetime(2026, 7, 20, 8, 30, tzinfo=UTC)
# 12:00 UTC — 21:00 KST / 12:00 UTC — morning for neither.
_MIDDAY_UTC = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


def _daily_brief_on() -> dict[str, dict[str, bool]]:
    return {"daily_brief": {"in_app": True}}


async def _seed_workspace(
    sf: async_sessionmaker[AsyncSession],
    *,
    ws: uuid.UUID,
    timezone: str = "UTC",
    matrix: dict[str, dict[str, bool]] | None = None,
) -> None:
    async with sf() as s:
        s.add(WorkspaceRow(id=ws, name="Test WS", timezone=timezone))
        if matrix is not None:
            s.add(NotificationPrefsRow(workspace_id=ws, matrix=matrix))
        await s.commit()


async def _seed_run(
    sf: async_sessionmaker[AsyncSession],
    *,
    ws: uuid.UUID,
    status: RunStatus,
    updated_at: datetime,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=status,
                payload={},
                updated_at=updated_at,
            )
        )
        await s.commit()
    return run_id


async def _seed_pending_decision(
    sf: async_sessionmaker[AsyncSession], *, ws: uuid.UUID, run_id: uuid.UUID
) -> None:
    async with sf() as s:
        s.add(
            Decision(
                run_id=run_id,
                workspace_id=ws,
                decision="ask_user",
                status=DecisionStatus.PENDING,
                payload={},
            )
        )
        await s.commit()


async def _rows(sf: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> list[NotificationEventRow]:
    async with sf() as s:
        return list(
            (
                await s.execute(
                    select(NotificationEventRow).where(NotificationEventRow.workspace_id == ws)
                )
            )
            .scalars()
            .all()
        )


async def test_morning_tick_emits_one_daily_brief_with_counts(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """[P] opted-in workspace + 2 shipped + 1 failed + 3 pending decisions,
    ticked at its local morning ⇒ exactly one daily_brief row whose body carries
    the counts."""
    ws = uuid.uuid4()
    await _seed_workspace(sf, ws=ws, timezone="UTC", matrix=_daily_brief_on())
    recent = _UTC_MORNING - timedelta(hours=1)
    await _seed_run(sf, ws=ws, status=RunStatus.SHIPPED, updated_at=recent)
    await _seed_run(sf, ws=ws, status=RunStatus.SHIPPED, updated_at=recent)
    await _seed_run(sf, ws=ws, status=RunStatus.FAILED, updated_at=recent)
    decision_run = await _seed_run(sf, ws=ws, status=RunStatus.RUNNING, updated_at=recent)
    for _ in range(3):
        await _seed_pending_decision(sf, ws=ws, run_id=decision_run)

    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _UTC_MORNING)
    emitted = await worker.run_once()

    assert emitted == 1
    rows = await _rows(sf, ws)
    assert len(rows) == 1
    row = rows[0]
    assert row.event == "daily_brief"
    assert row.status is NotificationStatus.PENDING
    assert row.dedupe_key == f"daily_brief:{ws}:2026-07-20"
    assert row.payload["link"] == "/brief"
    body = str(row.payload["body"])
    assert "2 shipped" in body
    assert "1 failed" in body
    assert "3 decision" in body


async def test_off_window_tick_emits_nothing(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A tick outside the local morning window produces no row."""
    ws = uuid.uuid4()
    await _seed_workspace(sf, ws=ws, timezone="UTC", matrix=_daily_brief_on())

    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _MIDDAY_UTC)
    emitted = await worker.run_once()

    assert emitted == 0
    assert await _rows(sf, ws) == []


async def test_second_tick_same_day_is_deduped_to_one_row(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The local-date dedupe key makes a re-tick the same day a DB no-op."""
    ws = uuid.uuid4()
    await _seed_workspace(sf, ws=ws, timezone="UTC", matrix=_daily_brief_on())
    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _UTC_MORNING)

    await worker.run_once()
    await worker.run_once()

    assert len(await _rows(sf, ws)) == 1


async def test_disabled_workspace_gets_no_brief(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """daily_brief off for every channel ⇒ no row even at local morning."""
    ws = uuid.uuid4()
    await _seed_workspace(sf, ws=ws, timezone="UTC", matrix={"daily_brief": {"in_app": False}})

    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _UTC_MORNING)
    emitted = await worker.run_once()

    assert emitted == 0
    assert await _rows(sf, ws) == []


async def test_no_prefs_row_defaults_off_no_brief(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A workspace that never opted in (no prefs row → default off) gets none."""
    ws = uuid.uuid4()
    await _seed_workspace(sf, ws=ws, timezone="UTC", matrix=None)

    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _UTC_MORNING)

    assert await worker.run_once() == 0
    assert await _rows(sf, ws) == []


async def test_morning_is_evaluated_in_the_workspace_timezone(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """For ONE wall-clock instant, a KST workspace briefs (its local 08:30) while
    a UTC workspace does not (its local 23:30)."""
    kst = uuid.uuid4()
    utc = uuid.uuid4()
    await _seed_workspace(sf, ws=kst, timezone="Asia/Seoul", matrix=_daily_brief_on())
    await _seed_workspace(sf, ws=utc, timezone="UTC", matrix=_daily_brief_on())

    worker = DailyBriefWorker(session_factory=sf, clock=lambda: _KST_MORNING_UTC)
    emitted = await worker.run_once()

    assert emitted == 1
    kst_rows = await _rows(sf, kst)
    assert len(kst_rows) == 1
    # KST local date is the NEXT day (23:30 UTC → 08:30 KST on the 21st).
    assert kst_rows[0].dedupe_key == f"daily_brief:{kst}:2026-07-21"
    assert await _rows(sf, utc) == []


def test_default_config_morning_window() -> None:
    cfg = DailyBriefWorkerConfig()
    assert cfg.morning_hour == 8
    assert cfg.poll_interval_s < 3600  # must poll more than once per morning hour
