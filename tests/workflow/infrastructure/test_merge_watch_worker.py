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

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from backend.workflow.application.runtime.merge_watch_server_freshen import (
    FreshnessTarget,
    freshen_in_clone,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    FreshenOutcome,
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
        self.close_calls: list[tuple[str, str, int]] = []

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        self.get_pr_calls.append((owner, repo, number))
        return dict(self._pr)

    async def merge_pr(
        self, owner: str, repo: str, number: int, *, method: str = "squash"
    ) -> MergeResult:
        self.merge_calls.append((owner, repo, number, method))
        return self._merge_result

    async def close_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        self.close_calls.append((owner, repo, number))
        return {"state": "closed"}


def _resolver_for(client: Any):  # noqa: ANN202
    # Takes the watched row's run id too (#681): the application-side resolver
    # scopes the binding to that run's product.
    async def _resolve(_session: AsyncSession, _workspace_id: uuid.UUID, _run_id: uuid.UUID) -> Any:
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


def _at(dt: datetime) -> datetime:
    """Compare row timestamps as instants, not as tz representations.

    A ``TIMESTAMPTZ`` read back from Postgres carries UTC; the same column on
    SQLite comes back naive. Assertions here are about *when*, so normalise
    before comparing rather than pinning the suite to one backend.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
        assert _at(fetched.next_poll_at) > _FIXED_NOW  # advanced by backoff
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
async def test_behind_or_dirty_pr_waits_when_freshness_unwired(mergeable_state: str) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row()
        await _seed(sf, row)
        client = _FakeClient(
            pr={"state": "open", "merged": False, "mergeable_state": mergeable_state}
        )
        # ``_worker`` injects NO freshness deps → the freshness merge cannot run;
        # the behind/dirty branch falls back to the old "wait" behavior.
        await _worker(sf, client).drain_once()
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.last_error == "awaiting_freshness_unwired"
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


# ---------------------------------------------------------------------------
# PR6 — behind/dirty freshness merge (REAL local git via a bare repo).
# ---------------------------------------------------------------------------
#
# The github REST client is faked (returns mergeable_state="behind"/"dirty"), but
# the freshness merge runs against a REAL local bare repo standing in for github
# — like tests/delivery/test_git_ops.py — so the clean-vs-conflict decision is
# authoritative, not mocked. The re-dispatch callback is a recording fake.


async def _run_git(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def _seed_bare(tmp_path: Path, name: str) -> Path:
    """A bare repo seeded on ``main`` with ``shared.txt`` + ``README.md``."""
    bare = tmp_path / f"{name}.git"
    await _run_git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / f"{name}-seed"
    ops = GitOps()
    await ops.clone(bare.as_uri(), seed, token=None, depth=0)
    (seed / "shared.txt").write_text("base line\n")
    (seed / "README.md").write_text("seed\n")
    await ops.commit_all(seed, "initial")
    await ops.push(seed, "main", token=None)
    return bare


async def _push_run_branch(bare: Path, dest: Path, branch: str, *, overlap: bool) -> None:
    """Create ``branch`` off main in ``dest`` (a shallow clone), commit a change,
    and push it. ``overlap`` edits ``shared.txt`` (→ conflict with a same-file
    base advance); else adds a new ``branch.txt`` (→ clean merge)."""
    ops = GitOps()
    await ops.clone(bare.as_uri(), dest, token=None, depth=1)
    await ops.checkout_new_branch(dest, branch)
    if overlap:
        (dest / "shared.txt").write_text("branch version\n")
    else:
        (dest / "branch.txt").write_text("from branch\n")
    await ops.commit_all(dest, "feat: branch change")
    await ops.push(dest, branch, token=None)


async def _advance_main(tmp_path: Path, bare: Path, *, overlap: bool) -> None:
    """Advance ``main`` after the branch forked. ``overlap`` edits the SAME
    ``shared.txt`` line (→ conflict); else adds ``base.txt`` (→ clean)."""
    ops = GitOps()
    work = tmp_path / f"advance-{uuid.uuid4().hex[:6]}"
    await ops.clone(bare.as_uri(), work, token=None, depth=0)
    if overlap:
        (work / "shared.txt").write_text("base advanced\n")
    else:
        (work / "base.txt").write_text("from base\n")
    await ops.commit_all(work, "base advance")
    await ops.push(work, "main", token=None)


class _RecordingRedispatch:
    """Records each conflict re-dispatch call (asserts it fires exactly once)."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, list[str], str, int]] = []

    async def __call__(
        self,
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None:
        self.calls.append((run_id, conflict_paths, base_branch, pr_number))


class _RecordingEscalate:
    """Records each conflict escalation call (asserts it fires exactly once)."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, list[str], str, int]] = []

    async def __call__(
        self,
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None:
        self.calls.append((run_id, conflict_paths, base_branch, pr_number))


def _freshness_for(target: FreshnessTarget | None, run_root: Path, git: GitOps):  # noqa: ANN202
    """The SERVER-side freshener with its binding resolution stubbed out.

    The git itself is the real implementation (``freshen_in_clone``) against real
    repositories — that is what these tests are for. Only the step that decrypts
    a token out of the database is replaced by the injected ``target``.
    """

    async def _freshen(
        _session: AsyncSession, _workspace_id: uuid.UUID, run_id: uuid.UUID, branch: str
    ) -> FreshenOutcome:
        if target is None:
            return FreshenOutcome(status="unavailable", base_branch="")
        return await freshen_in_clone(
            git=git, clone=run_root / str(run_id), branch=branch, target=target
        )

    return _freshen


def _freshness_worker(
    sf: async_sessionmaker[AsyncSession],
    client: Any,
    *,
    target: FreshnessTarget | None,
    redispatch: Any,
    run_root: Path,
    git_ops: GitOps | None = None,
    escalate: Any = None,
    config: MergeWatchWorkerConfig | None = None,
    now: datetime = _FIXED_NOW,
) -> MergeWatchWorker:
    return MergeWatchWorker(
        session_factory=sf,
        client_resolver=_resolver_for(client),
        branch_freshener=_freshness_for(target, run_root, git_ops or GitOps()),
        redispatch_conflict=redispatch,
        escalate_conflict=escalate,
        config=config or MergeWatchWorkerConfig(poll_interval_s=30.0),
        now=lambda: now,
    )


async def test_behind_pr_clean_freshness_merge_pushes_and_waits(tmp_path: Path) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-clean")
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "clean")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=False)
        await _advance_main(tmp_path, bare, overlap=False)  # non-overlapping base change

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "behind"})
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI  # re-CI on the fresh head
        assert fetched.last_error == "freshened"
        assert redispatch.calls == []  # clean → no hand-off
        assert client.merge_calls == []  # freshness NEVER squash-merges the PR
        # The freshened branch was PUSHED — the remote branch now carries the
        # merged-in base change alongside the branch change.
        files = await _run_git("ls-tree", "-r", "--name-only", row.branch, cwd=bare)
        assert "branch.txt" in files
        assert "base.txt" in files


async def test_dirty_pr_conflict_marks_needs_resolution_and_redispatches_once(
    tmp_path: Path,
) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-conflict", pr_number=11)
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "conflict")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=True)
        await _advance_main(tmp_path, bare, overlap=True)  # same-file base change → conflict

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "dirty"})
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.conflict_dispatched is True
        assert fetched.last_error == "merge_conflict"
        assert client.merge_calls == []
        # Re-dispatched EXACTLY once with the conflict path / base / pr.
        assert len(redispatch.calls) == 1
        run_id, paths, base, pr = redispatch.calls[0]
        assert run_id == row.run_id
        assert paths == ["shared.txt"]
        assert base == "main"
        assert pr == 11


async def test_conflict_already_dispatched_does_not_redispatch_again(tmp_path: Path) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        # A row that already handed a conflict to the agent (loop guard set).
        row = _row(repo="acme/fresh-guard")
        row.conflict_dispatched = True
        row.status = MergeWatchStatus.NEEDS_RESOLUTION
        await _seed(sf, row)

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "dirty"})
        redispatch = _RecordingRedispatch()
        # No repo needed — the guard short-circuits BEFORE any git/resolver work.
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.last_error == "awaiting_conflict_resolution"
        assert redispatch.calls == []  # loop guard — no second dispatch


async def test_missing_clone_is_recloned_then_freshness_merge_runs(tmp_path: Path) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-reclone")
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "reclone")
        # Push the run branch to the remote via a THROWAWAY clone (elsewhere) — the
        # run workspace at run_root/<run_id> is deliberately absent (reaped).
        await _push_run_branch(bare, tmp_path / "throwaway", row.branch, overlap=False)
        await _advance_main(tmp_path, bare, overlap=False)
        clone = run_root / str(row.run_id)
        assert not clone.exists()

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "behind"})
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.last_error == "freshened"
        assert (clone / ".git").exists()  # re-cloned into place
        files = await _run_git("ls-tree", "-r", "--name-only", row.branch, cwd=bare)
        assert "branch.txt" in files
        assert "base.txt" in files


async def test_freshness_repo_busy_propagates_and_leaves_no_partial_state(
    tmp_path: Path,
) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-busy")
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "busy")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=False)
        await _advance_main(tmp_path, bare, overlap=False)

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "behind"})
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root
        )
        # Hold the per-repo lock on a separate session so the freshness step loses
        # it (GithubRepoBusy) — the row is retried next tick, no partial state.
        async with sf() as holder, github_repo_lock(holder, "acme/fresh-busy"):
            processed = await worker.drain_once()

        assert processed == 1
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI  # unchanged (reserved)
        assert redispatch.calls == []
        # The branch was NOT freshened/pushed (base.txt never merged into it).
        files = await _run_git("ls-tree", "-r", "--name-only", row.branch, cwd=bare)
        assert "base.txt" not in files


class _PushFailsGitOps(GitOps):
    """A GitOps whose ``push`` always fails — to exercise the git-failure path."""

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        raise GitError("simulated push rejection")


async def test_freshness_git_failure_backs_off_to_pending_ci(tmp_path: Path) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-gitfail")
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "gitfail")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=False)
        await _advance_main(tmp_path, bare, overlap=False)

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "behind"})
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        # The merge is clean but the PUSH fails → back off, don't crash.
        worker = _freshness_worker(
            sf,
            client,
            target=target,
            redispatch=redispatch,
            run_root=run_root,
            git_ops=_PushFailsGitOps(),
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.last_error == "freshen_failed"
        assert _at(fetched.next_poll_at) > _FIXED_NOW  # backed off
        assert redispatch.calls == []


async def test_freshness_no_target_marks_failed(tmp_path: Path) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-notarget")
        await _seed(sf, row)

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "behind"})
        redispatch = _RecordingRedispatch()
        # Resolver returns None (connector removed) → the row can never freshen.
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.FAILED
        assert fetched.last_error == "github_binding_unavailable"
        assert redispatch.calls == []


# ---------------------------------------------------------------------------
# PR7 — the conflict-resolution loop CLOSES: a resolved PR re-enters the merge
# flow; an unresolved one waits without infinite re-dispatch; a discarded run's
# orphaned PR is closed.
# ---------------------------------------------------------------------------


async def test_needs_resolution_now_clean_proceeds_to_merge() -> None:
    """The agent resolved + re-pushed → the PR is NOW clean → it merges (the
    clean branch of the state machine handles a needs_resolution re-poll)."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row(status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "oldsha"
        await _seed(sf, row)
        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "clean",
                "head": {"sha": "newsha"},
            }
        )
        assert await _worker(sf, client).drain_once() == 1
        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.MERGED
        assert client.merge_calls == [("octocat", "hello-world", 7, "squash")]


async def test_needs_resolution_still_dirty_same_head_waits_no_redispatch(tmp_path: Path) -> None:
    """STILL conflicting on the SAME head (agent hasn't re-pushed) → keep waiting;
    the guard holds, no second dispatch, no freshness work attempted."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/still-same", status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "samehead"
        await _seed(sf, row)

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "samehead"},  # unchanged since dispatch
            }
        )
        redispatch = _RecordingRedispatch()
        # target=None + no repo work: the guard short-circuits before any git.
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.last_error == "awaiting_conflict_resolution"
        assert fetched.conflict_head_sha == "samehead"  # unchanged
        assert redispatch.calls == []  # loop guard — no second dispatch
        assert client.merge_calls == []


async def test_needs_resolution_changed_head_still_conflict_redispatches_once(
    tmp_path: Path,
) -> None:
    """The agent re-pushed (head advanced) but produced a NEW conflicting state →
    the freshness merge re-runs, the guard resets, and the agent is re-dispatched
    once for the new head (which is recorded)."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/changed-head", pr_number=13, status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "oldhead"  # last dispatched on this head
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "changed")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=True)
        await _advance_main(tmp_path, bare, overlap=True)  # same-file → still conflicts

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "newhead"},  # advanced since dispatch
            }
        )
        redispatch = _RecordingRedispatch()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.conflict_dispatched is True
        assert fetched.conflict_head_sha == "newhead"  # pinned to the new head
        assert fetched.last_error == "merge_conflict"
        # A NEW conflict on an advanced head resets the retry clock: attempts
        # back to 1 (dispatch #1 for this head) + freshly stamped.
        assert fetched.conflict_attempts == 1
        assert _at(fetched.conflict_dispatched_at) == _FIXED_NOW
        # Re-dispatched exactly once more for the NEW conflicting state.
        assert len(redispatch.calls) == 1
        assert redispatch.calls[0][0] == row.run_id
        assert redispatch.calls[0][3] == 13


async def test_cancelled_run_closes_orphaned_pr_and_abandons() -> None:
    """Founder discarded the resolving run (→ CANCELLED): the worker closes the
    now-orphaned PR and stops watching — the loop closes without a merge."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = _row(repo="acme/discarded", pr_number=21, status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        await _seed(sf, row)
        # The originating run was cancelled by the founder's discard.
        async with sf() as s:
            s.add(
                ExecutionRun(
                    id=row.run_id,
                    workspace_id=row.workspace_id,
                    status=RunStatus.CANCELLED,
                    payload={},
                    created_at=_FIXED_NOW,
                )
            )
            await s.commit()

        client = _FakeClient(
            pr={"state": "open", "merged": False, "mergeable_state": "dirty", "head": {"sha": "h"}}
        )
        assert await _worker(sf, client).drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.ABANDONED
        assert fetched.last_error == "run_cancelled"
        assert client.close_calls == [("acme", "discarded", 21)]
        assert client.merge_calls == []  # a cancelled run's PR is never merged


# ---------------------------------------------------------------------------
# Conflict-robustness — retry-then-escalate so a stalled re-drive never wedges
# the row in ``needs_resolution`` forever (the live-soak bug).
# ---------------------------------------------------------------------------


async def test_conflict_within_deadline_keeps_waiting_no_recovery(tmp_path: Path) -> None:
    """Head UNCHANGED (agent hasn't re-pushed) but the resolution deadline has
    NOT elapsed → keep parking, give the agent time. No retry, no escalation."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/within-deadline", status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "samehead"
        row.conflict_attempts = 1
        row.conflict_dispatched_at = _FIXED_NOW - timedelta(seconds=60)  # < 900s deadline
        await _seed(sf, row)

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "samehead"},
            }
        )
        redispatch = _RecordingRedispatch()
        escalate = _RecordingEscalate()
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root, escalate=escalate
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.last_error == "awaiting_conflict_resolution"
        assert fetched.conflict_attempts == 1  # unchanged
        assert redispatch.calls == []  # no retry
        assert escalate.calls == []  # no escalation


async def test_conflict_deadline_exceeded_attempts_remain_redispatches_retry(
    tmp_path: Path,
) -> None:
    """Head UNCHANGED past the deadline (re-drive stalled/failed) with attempts
    remaining → RE-DISPATCH again: the callback fires, ``conflict_attempts`` is
    bumped, ``conflict_dispatched_at`` re-stamped, still ``needs_resolution``."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/retry", pr_number=17, status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "samehead"
        row.conflict_attempts = 1  # < max (2)
        row.conflict_dispatched_at = _FIXED_NOW - timedelta(seconds=1000)  # > 900s deadline
        await _seed(sf, row)

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "samehead"},
            }
        )
        redispatch = _RecordingRedispatch()
        escalate = _RecordingEscalate()
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root, escalate=escalate
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.last_error == "conflict_redispatch_retry"
        assert fetched.conflict_attempts == 2  # incremented
        assert _at(fetched.conflict_dispatched_at) == _FIXED_NOW  # re-stamped to now
        assert len(redispatch.calls) == 1  # re-dispatched again
        assert redispatch.calls[0][0] == row.run_id
        assert redispatch.calls[0][3] == 17
        assert escalate.calls == []  # not yet exhausted


async def test_conflict_deadline_exceeded_attempts_exhausted_escalates(tmp_path: Path) -> None:
    """Head UNCHANGED past the deadline with attempts EXHAUSTED → escalate to a
    founder Decision exactly once, mark the row FAILED so it stops polling."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/escalate", pr_number=23, status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "samehead"
        row.conflict_attempts = 2  # == max (2)
        row.conflict_dispatched_at = _FIXED_NOW - timedelta(seconds=1000)  # > 900s deadline
        row.base_branch = "develop"
        await _seed(sf, row)

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "samehead"},
            }
        )
        redispatch = _RecordingRedispatch()
        escalate = _RecordingEscalate()
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root, escalate=escalate
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.FAILED  # terminal — stops polling
        assert fetched.last_error == "conflict_unresolved_escalated"
        assert redispatch.calls == []  # no further re-dispatch
        assert len(escalate.calls) == 1  # escalated exactly once
        run_id, _paths, base, pr = escalate.calls[0]
        assert run_id == row.run_id
        assert base == "develop"  # base branch threaded to the founder Decision
        assert pr == 23

        # A FAILED row is not claimable → a second poll does nothing (no more
        # escalation, no re-dispatch): the loop has terminated, not wedged.
        assert await worker.drain_once() == 0
        assert len(escalate.calls) == 1
        assert redispatch.calls == []


async def test_conflict_dispatched_at_none_keeps_waiting(tmp_path: Path) -> None:
    """A legacy dispatched row with NO ``conflict_dispatched_at`` (pre-migration)
    can't measure elapsed time → treat as within deadline (keep waiting) rather
    than mis-escalate. The next fresh dispatch stamps it and the clock starts."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/legacy", status=MergeWatchStatus.NEEDS_RESOLUTION)
        row.conflict_dispatched = True
        row.conflict_head_sha = "samehead"
        row.conflict_attempts = 2  # even at max — but no timestamp → can't escalate
        row.conflict_dispatched_at = None
        await _seed(sf, row)

        client = _FakeClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "dirty",
                "head": {"sha": "samehead"},
            }
        )
        redispatch = _RecordingRedispatch()
        escalate = _RecordingEscalate()
        worker = _freshness_worker(
            sf, client, target=None, redispatch=redispatch, run_root=run_root, escalate=escalate
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.last_error == "awaiting_conflict_resolution"
        assert redispatch.calls == []
        assert escalate.calls == []


async def test_fresh_conflict_stamps_attempts_and_dispatched_at(tmp_path: Path) -> None:
    """A FIRST-time conflict dispatch stamps ``conflict_attempts=1`` +
    ``conflict_dispatched_at=now`` so the deadline clock starts for real."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_root = tmp_path / "runs"
        row = _row(repo="acme/fresh-stamp", pr_number=31)
        await _seed(sf, row)

        bare = await _seed_bare(tmp_path, "freshstamp")
        clone = run_root / str(row.run_id)
        await _push_run_branch(bare, clone, row.branch, overlap=True)
        await _advance_main(tmp_path, bare, overlap=True)  # same-file → conflict

        client = _FakeClient(pr={"state": "open", "merged": False, "mergeable_state": "dirty"})
        redispatch = _RecordingRedispatch()
        escalate = _RecordingEscalate()
        target = FreshnessTarget(
            repo=row.repo, base_branch="main", token=None, remote_url=bare.as_uri()
        )
        worker = _freshness_worker(
            sf, client, target=target, redispatch=redispatch, run_root=run_root, escalate=escalate
        )
        assert await worker.drain_once() == 1

        fetched = await _fetch(sf, row.id)
        assert fetched.status is MergeWatchStatus.NEEDS_RESOLUTION
        assert fetched.conflict_dispatched is True
        assert fetched.conflict_attempts == 1  # first dispatch for this head
        assert _at(fetched.conflict_dispatched_at) == _FIXED_NOW  # clock started
        assert len(redispatch.calls) == 1
        assert escalate.calls == []


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
