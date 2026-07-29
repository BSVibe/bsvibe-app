"""PR4 — MergeWatchWorker CI-green auto-merge state machine + enqueue seam.

Exercises :mod:`backend.workflow.infrastructure.workers.merge_watch_worker`:

* the per-row state machine (clean→squash-merge, blocked/unstable→wait,
  behind/dirty→NOT merged in PR4, deadline→failed, closed→abandoned, already
  merged→merged idempotent, head_changed/not_mergeable→back to pending_ci),
* per-repo serialization (a held repo lock ⇒ ``GithubRepoBusy`` ⇒ the row waits,
  merge NOT attempted — mirrors the product-workspace lock contract),
* the enqueue seam (``enqueue_merge_watch``): flag ON inserts one ``pending_ci``
  row with the right repo/pr_number/branch/deadline; flag OFF inserts nothing.

Real Postgres (``BSVIBE_DATABASE_URL``) for the row / claim / lock persistence;
the GitHub client is a fake and the clock is injected fixed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.data import Base
from backend.storage.github_repo_lock import github_repo_lock
from backend.workflow.application.delivery.connector_dispatch._github import GithubDeliveryDeps
from backend.workflow.application.delivery.connector_dispatch._merge_watch import (
    enqueue_merge_watch,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import GithubBinding
from backend.workflow.infrastructure.delivery.git_ops import GitOps
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    MergeWatchWorker,
    MergeWatchWorkerConfig,
)
from plugin.github.client import MergeResult
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

_FIXED_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeClient:
    """A recording GitHub client double.

    ``pr`` is the ``get_pr`` payload (returned for every ``get_pr`` call, incl.
    the under-lock re-confirm). ``merge_result`` is what ``merge_pr`` returns.
    """

    def __init__(self, *, pr: dict[str, Any], merge_result: MergeResult | None = None) -> None:
        self._pr = pr
        self._merge_result = merge_result or MergeResult(
            status="merged", merged=True, sha="deadbee"
        )
        self.get_pr_calls: list[tuple[str, str, int]] = []
        self.merge_calls: list[tuple[str, str, int, str]] = []

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        self.get_pr_calls.append((owner, repo, number))
        return dict(self._pr)

    async def merge_pr(
        self, owner: str, repo: str, number: int, *, method: str = "squash"
    ) -> MergeResult:
        self.merge_calls.append((owner, repo, number, method))
        return self._merge_result


def _resolver_for(client: Any):  # noqa: ANN202
    async def _resolve(_session: AsyncSession, _workspace_id: uuid.UUID) -> Any:
        return client

    return _resolve


def _worker(
    sf: async_sessionmaker[AsyncSession], client: Any, *, now: datetime = _FIXED_NOW
) -> MergeWatchWorker:
    return MergeWatchWorker(
        session_factory=sf,
        client_resolver=_resolver_for(client),
        config=MergeWatchWorkerConfig(poll_interval_s=30.0),
        now=lambda: now,
    )


def _row(
    *,
    repo: str = "octocat/hello-world",
    pr_number: int = 7,
    status: MergeWatchStatus = MergeWatchStatus.PENDING_CI,
    attempts: int = 0,
    next_poll_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> GithubMergeWatchRow:
    return GithubMergeWatchRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        repo=repo,
        pr_number=pr_number,
        branch="bsvibe/run-abcd1234",
        base_branch="main",
        status=status,
        attempts=attempts,
        next_poll_at=next_poll_at or (_FIXED_NOW - timedelta(seconds=1)),
        deadline_at=deadline_at or (_FIXED_NOW + timedelta(hours=1)),
        conflict_dispatched=False,
        created_at=_FIXED_NOW,
    )


async def _seed(sf: async_sessionmaker[AsyncSession], row: GithubMergeWatchRow) -> None:
    async with sf() as session:
        session.add(row)
        await session.commit()


async def _fetch(sf: async_sessionmaker[AsyncSession], row_id: uuid.UUID) -> GithubMergeWatchRow:
    async with sf() as session:
        fetched = await session.get(GithubMergeWatchRow, row_id)
        assert fetched is not None
        return fetched


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


async def test_clean_pr_is_squash_merged() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "clean"})
        assert await _worker(sf, client).drain_once() == 1
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.MERGED
        assert client.merge_calls == [("octocat", "hello-world", 7, "squash")]


async def test_blocked_pr_waits_pending_ci_with_backoff() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row(attempts=0)
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "blocked"})
        assert await _worker(sf, client).drain_once() == 1
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.attempts == 1
        assert fetched.next_poll_at > _FIXED_NOW  # advanced by backoff
        assert client.merge_calls == []  # never merged


async def test_unstable_pr_not_merged() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "unstable"})
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert client.merge_calls == []


@pytest.mark.parametrize("mergeable_state", ["behind", "dirty"])
async def test_behind_or_dirty_pr_is_not_merged_in_pr4(mergeable_state: str) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(
            pr={"state": "open", "merged": False, "mergeable_state": mergeable_state}
        )
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        # PR4 defers freshness/conflict recovery to PR6 — the row waits, unmerged.
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.last_error == "awaiting_freshness (pr6)"
        assert client.merge_calls == []


async def test_deadline_exceeded_marks_failed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        # deadline already in the past + CI still pending (blocked).
        row = _row(deadline_at=_FIXED_NOW - timedelta(minutes=1))
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "blocked"})
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.FAILED
        assert fetched.last_error == "ci_deadline_exceeded"
        assert client.merge_calls == []


async def test_closed_unmerged_pr_is_abandoned() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "closed", "merged": False, "mergeable_state": "dirty"})
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.ABANDONED
        assert client.merge_calls == []


async def test_already_merged_pr_is_marked_merged_idempotent() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        # merged True even though mergeable_state is not clean — someone/we merged
        # it already; re-claim must map straight to merged (no second merge call).
        client = _FakeClient(pr={"state": "closed", "merged": True, "mergeable_state": "unknown"})
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.MERGED
        assert client.merge_calls == []


@pytest.mark.parametrize("status", ["not_mergeable", "head_changed"])
async def test_merge_rejected_falls_back_to_pending_ci(status: str) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(
            pr={"state": "open", "merged": False, "mergeable_state": "clean"},
            merge_result=MergeResult(status=status, merged=False),  # type: ignore[arg-type]
        )
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        # The state changed under us — back to pending_ci, no crash.
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.last_error == status
        assert len(client.merge_calls) == 1  # attempted once


async def test_no_due_rows_returns_zero() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        # next_poll far in the future → not claimable.
        row = _row(next_poll_at=_FIXED_NOW + timedelta(hours=1))
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "clean"})
        assert await _worker(sf, client).drain_once() == 0
        assert client.get_pr_calls == []


# ---------------------------------------------------------------------------
# Per-repo serialization — a held lock ⇒ the row waits, merge NOT attempted.
# ---------------------------------------------------------------------------


async def test_repo_lock_serializes_merge() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row(repo="acme/widget")
        await _seed(sf, row)
        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "clean"})

        # Hold the per-repo merge lock on a SEPARATE session so the worker's
        # merge step loses it (GithubRepoBusy) and defers — mirroring the
        # product-workspace lock contract.
        async with sf() as holder, github_repo_lock(holder, "acme/widget"):
            processed = await _worker(sf, client).drain_once()

        assert processed == 1  # the row was claimed + processed (then deferred)
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI  # not merged
        assert client.merge_calls == []  # merge NOT attempted under contention


# ---------------------------------------------------------------------------
# Enqueue seam — enqueue_merge_watch (flag ON inserts one row, OFF inserts none)
# ---------------------------------------------------------------------------


def _deps(sf: async_sessionmaker[AsyncSession], *, flag: bool) -> GithubDeliveryDeps:
    settings = get_settings().model_copy(
        update={
            "github_auto_merge_enabled": flag,
            "github_auto_merge_ci_deadline_s": 1800.0,
        }
    )
    return GithubDeliveryDeps(
        cipher=None,  # type: ignore[arg-type]  # unused by the enqueue helper
        plugins_by_name={},
        workspace_root=None,
        git_ops=GitOps(),
        remote_url_for=lambda r: r,
        runner=None,  # type: ignore[arg-type]  # unused by the enqueue helper
        session_factory=sf,
        settings=settings,
    )


def _binding() -> GithubBinding:
    from backend.connectors.db import ConnectorAccountRow

    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connector="github",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="x",
        delivery_config={"repo": "owner/name"},
        is_active=True,
    )
    return GithubBinding(account=account, repo="owner/name", base_branch="develop")


async def test_enqueue_inserts_pending_ci_row_when_flag_on() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id, deliverable_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await enqueue_merge_watch(
            _deps(sf, flag=True),
            binding=_binding(),
            workspace_id=ws,
            run_id=run_id,
            deliverable_id=deliverable_id,
            branch="bsvibe/run-1234abcd",
            pr_number=7,
        )
        async with sf() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()
        assert len(rows) == 1
        r = rows[0]
        assert r.status is MergeWatchStatus.PENDING_CI
        assert r.repo == "owner/name"
        assert r.pr_number == 7
        assert r.branch == "bsvibe/run-1234abcd"
        assert r.base_branch == "develop"  # from the binding
        assert r.workspace_id == ws
        assert r.run_id == run_id
        assert r.deliverable_id == deliverable_id
        # deadline_at = created + ci_deadline_s (1800s) — a ~30min window.
        assert timedelta(minutes=25) < (r.deadline_at - r.next_poll_at) < timedelta(minutes=35)


async def test_enqueue_is_noop_when_flag_off() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await enqueue_merge_watch(
            _deps(sf, flag=False),
            binding=_binding(),
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            branch="b",
            pr_number=1,
        )
        async with sf() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()
        assert rows == []


async def test_enqueue_is_noop_when_settings_absent() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        deps = _deps(sf, flag=True)
        deps.settings = None  # no settings threaded → treat as flag off
        await enqueue_merge_watch(
            deps,
            binding=_binding(),
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            deliverable_id=uuid.uuid4(),
            branch="b",
            pr_number=1,
        )
        async with sf() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(GithubMergeWatchRow))).scalars().all()
        assert rows == []
