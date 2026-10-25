"""머지 감시가 스스로 포기하고 돌아올 길이 없던 세 구멍 (2026-10-23).

* 자기가 갱신한 PR(freshen)을 원래 마감으로 포기한다 (PR #1222).
* CI 가 빨개지면 원래 런에 고치게 보내지 않고 Decision 만 올린다 (PR #1218, #1223).
* 포기(``failed``)한 뒤 PR 이 머지돼도 런이 모른다 (런 ``7709a7a2`` / ``6fa72ec1``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.application.intake.ci_failure import (
    ci_failure_messages,
    consume_ci_failure,
    revive_merge_watch,
    send_ci_failure_to_run,
)
from backend.workflow.application.run_delivery_resolution import AWAITING_MERGE_KEY
from backend.workflow.application.runtime.merge_watch_runtime import (
    build_merge_watch_ci_redispatch,
    build_merge_watch_pr_concluded,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    FreshenOutcome,
    MergeWatchWorker,
    MergeWatchWorkerConfig,
    _log_excerpt,
)
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 10, 23, 8, 13, tzinfo=UTC)
_CI_DEADLINE_S = 3600.0
_HEAD = "a" * 40


class _Github:
    """A GitHub whose PR and check-runs the test sets."""

    def __init__(self, *, pr: dict[str, Any], checks: list[dict[str, Any]] | None = None) -> None:
        self.pr = pr
        self.checks = checks or []
        self.merge_calls = 0
        self.job_log: str | Exception = RuntimeError("no job log in this test")
        self.job_ids: list[int] = []

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return dict(self.pr)

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        return self.checks

    async def get_job_log(self, owner: str, repo: str, job_id: int) -> str:
        self.job_ids.append(job_id)
        if isinstance(self.job_log, Exception):
            raise self.job_log
        return self.job_log

    async def close_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return {"state": "closed"}


def _open_pr(mergeable_state: str, head: str = _HEAD) -> dict[str, Any]:
    return {
        "state": "open",
        "merged": False,
        "mergeable_state": mergeable_state,
        "head": {"sha": head},
    }


def _red_check() -> list[dict[str, Any]]:
    return [
        {
            "name": "lint-and-test",
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "2 failed", "summary": "tests/test_x.py::test_y FAILED"},
        },
        {"name": "pwa", "status": "completed", "conclusion": "success"},
    ]


async def _seed_watch(
    sf: async_sessionmaker[AsyncSession],
    *,
    status: MergeWatchStatus = MergeWatchStatus.PENDING_CI,
    deadline_at: datetime | None = None,
    run_status: RunStatus | None = None,
    run_payload: dict[str, Any] | None = None,
    **columns: Any,
) -> GithubMergeWatchRow:
    ws, run_id = uuid.uuid4(), uuid.uuid4()
    row = GithubMergeWatchRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        run_id=run_id,
        deliverable_id=uuid.uuid4(),
        repo="acme/app",
        pr_number=1222,
        branch=f"bsvibe/run-{run_id}",
        base_branch="main",
        status=status,
        attempts=0,
        next_poll_at=_NOW - timedelta(hours=2),
        deadline_at=deadline_at or (_NOW + timedelta(hours=1)),
        **columns,
    )
    async with sf() as session:
        async with workspace_session_scope(session, ws):
            if run_status is not None:
                session.add(
                    ExecutionRun(
                        id=run_id,
                        workspace_id=ws,
                        status=run_status,
                        payload=run_payload or {"intent_text": "x"},
                    )
                )
            session.add(row)
            await session.commit()
    return row


async def _row(sf: async_sessionmaker[AsyncSession], row: GithubMergeWatchRow) -> Any:
    async with sf() as session:
        fetched = await session.get(GithubMergeWatchRow, row.id)
        assert fetched is not None
        return fetched


async def _run(sf: async_sessionmaker[AsyncSession], row: GithubMergeWatchRow) -> ExecutionRun:
    async with sf() as session:
        async with workspace_session_scope(session, row.workspace_id):
            run = await session.get(ExecutionRun, row.run_id)
            assert run is not None
            return run


def _at(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class _Recorder:
    def __init__(self) -> None:
        self.stalls: list[dict[str, Any]] = []
        self.ci: list[dict[str, Any]] = []

    async def stall(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        self.stalls.append(kwargs)

    async def redispatch_ci(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        self.ci.append(kwargs)

    async def redispatch_conflict(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        raise AssertionError("no conflict in this test")

    async def freshen(self, *_a: Any) -> FreshenOutcome:
        return FreshenOutcome(status="clean", base_branch="main")


def _worker(
    sf: async_sessionmaker[AsyncSession],
    github: _Github,
    rec: _Recorder,
    *,
    now: datetime = _NOW,
    **extra: Any,
) -> MergeWatchWorker:
    async def _resolve(*_a: Any) -> _Github:
        return github

    return MergeWatchWorker(
        session_factory=sf,
        client_resolver=_resolve,  # type: ignore[arg-type]
        escalate_stall=rec.stall,
        redispatch_ci=rec.redispatch_ci,
        config=MergeWatchWorkerConfig(ci_deadline_s=_CI_DEADLINE_S, ci_fix_max_redispatch=2),
        now=lambda: now,
        **extra,
    )


# ── 1. a head the watch itself moved gets a fresh deadline ──────────────────────────────


async def test_a_freshened_pr_is_not_given_up_on_at_its_old_deadline() -> None:
    """PR #1222: opened 07:11 (+1h), freshened 07:48 (CI restarts, ~29min), abandoned 08:13."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        freshened_at = _NOW - timedelta(minutes=25)
        row = await _seed_watch(sf, deadline_at=freshened_at - timedelta(seconds=1))
        rec = _Recorder()
        github = _Github(pr=_open_pr("behind", head="old"))
        worker = _worker(
            sf, github, rec, now=freshened_at, branch_freshener=rec.freshen,
            redispatch_conflict=rec.redispatch_conflict,
        )  # fmt: skip
        await worker.drain_once()

        after_freshen = await _row(sf, row)
        assert after_freshen.status is MergeWatchStatus.PENDING_CI
        assert _at(after_freshen.deadline_at) == freshened_at + timedelta(seconds=_CI_DEADLINE_S)

        # 25 minutes later the new head's CI is still running: the OLD deadline has passed.
        github.pr = _open_pr("blocked", head="new")
        later = _worker(sf, github, rec, now=_NOW + timedelta(seconds=300))
        await later.drain_once()

        assert rec.stalls == [], "CI the watch restarted itself must not be abandoned"
        assert (await _row(sf, row)).status is MergeWatchStatus.PENDING_CI


async def test_a_new_head_restarts_the_deadline_even_when_someone_else_pushed_it() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, deadline_at=_NOW - timedelta(minutes=5), head_sha="old")
        rec = _Recorder()
        await _worker(sf, _Github(pr=_open_pr("blocked", head="new")), rec).drain_once()

        fetched = await _row(sf, row)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.head_sha == "new"
        assert _at(fetched.deadline_at) == _NOW + timedelta(seconds=_CI_DEADLINE_S)
        assert rec.stalls == []


async def test_an_unmoved_head_still_hits_its_deadline() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, deadline_at=_NOW - timedelta(minutes=5), head_sha=_HEAD)
        rec = _Recorder()
        await _worker(sf, _Github(pr=_open_pr("blocked")), rec).drain_once()

        assert (await _row(sf, row)).last_error == "ci_deadline_exceeded"
        assert [s["reason"] for s in rec.stalls] == ["ci_deadline_exceeded"]


# ── 2. red CI goes back to the original run ─────────────────────────────────────────────


async def test_red_ci_is_sent_back_to_the_run_with_the_failed_check_names() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, head_sha=_HEAD, ci_red_head_sha=_HEAD)  # second red look
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_red_check())
        await _worker(sf, github, rec).drain_once()

        assert rec.stalls == [], "the founder is not asked while the run can still fix it"
        [sent] = rec.ci
        assert [c["name"] for c in sent["failed_checks"]] == ["lint-and-test"]
        assert "FAILED" in sent["failed_checks"][0]["summary"]
        assert sent["head_sha"] == _HEAD
        fetched = await _row(sf, row)
        assert fetched.status is MergeWatchStatus.PENDING_CI, "still watched — the fix will land"
        assert fetched.ci_fix_attempts == 1
        assert fetched.ci_fix_head_sha == _HEAD
        assert _at(fetched.deadline_at) == _NOW + timedelta(seconds=_CI_DEADLINE_S)


_ACTIONS_LOG = "\n".join(
    [
        "2026-10-23T08:00:00.1234567Z ##[group]Run pytest",
        "2026-10-23T08:00:01.0000000Z collected 900 items",
        "2026-10-23T08:00:09.0000000Z =========================== short test summary info ============================",
        "2026-10-23T08:00:09.0000000Z FAILED tests/test_guard.py::test_every_decision_has_a_way_out - AssertionError",
        "2026-10-23T08:00:09.0000000Z ======================== 1 failed, 899 passed in 8.1s =========================",
    ]
)


def _actions_check() -> list[dict[str, Any]]:
    [check, other] = _red_check()
    # check-run id != job id here on purpose: the job id comes from details_url.
    check = {
        **check,
        "id": 111,
        "output": {"title": None, "summary": None},
        "details_url": "https://github.com/o/r/actions/runs/777/job/4242",
    }
    return [check, other]


async def test_the_actions_job_log_excerpt_reaches_the_runs_turn_message() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf, head_sha=_HEAD, ci_red_head_sha=_HEAD, run_status=RunStatus.REVIEW_READY
        )
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_actions_check())
        github.job_log = _ACTIONS_LOG
        await _worker(sf, github, rec).drain_once()

        assert github.job_ids == [4242]
        [sent] = rec.ci
        [check] = sent["failed_checks"]
        assert "FAILED tests/test_guard.py::test_every_decision_has_a_way_out" in check["summary"]
        assert "2026-10-23T" not in check["summary"], "log timestamps are noise"
        assert "collected 900 items" not in check["summary"], "only the failing part"

        with workspace_scope(row.workspace_id):
            await build_merge_watch_ci_redispatch(session_factory=sf)(
                row.run_id, failed_checks=sent["failed_checks"], pr_number=1222, head_sha=_HEAD
            )
        [message] = ci_failure_messages(await _run(sf, row))
        assert "test_every_decision_has_a_way_out" in message["content"]


async def test_the_job_log_is_not_downloaded_on_the_first_red_look() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_watch(sf, head_sha=_HEAD)  # first red look
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_actions_check())
        github.job_log = _ACTIONS_LOG
        await _worker(sf, github, rec).drain_once()

        assert rec.ci == [] and rec.stalls == []
        assert github.job_ids == []


async def test_the_job_log_is_not_downloaded_while_awaiting_the_fix() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_watch(
            sf, head_sha=_HEAD, ci_red_head_sha=_HEAD, ci_fix_attempts=1, ci_fix_head_sha=_HEAD
        )
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_actions_check())
        github.job_log = _ACTIONS_LOG
        await _worker(sf, github, rec).drain_once()

        assert rec.ci == [] and rec.stalls == []
        assert github.job_ids == []


async def test_the_job_log_is_downloaded_when_giving_up() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_watch(sf, head_sha=_HEAD, ci_red_head_sha=_HEAD, ci_fix_attempts=2)
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_actions_check())
        github.job_log = _ACTIONS_LOG
        await _worker(sf, github, rec).drain_once()

        assert github.job_ids == [4242]
        [stall] = rec.stalls
        assert "test_every_decision_has_a_way_out" in stall["failed_checks"][0]["summary"]
        assert "details_url" not in stall["failed_checks"][0]


async def test_an_unreadable_job_log_sends_the_check_name_alone() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_watch(sf, head_sha=_HEAD, ci_red_head_sha=_HEAD)
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_actions_check())
        github.job_log = PermissionError("403 Resource not accessible by integration")
        await _worker(sf, github, rec).drain_once()

        [sent] = rec.ci
        assert sent["failed_checks"] == [
            {"name": "lint-and-test", "conclusion": "failure", "summary": ""}
        ]


async def test_a_log_without_failure_lines_falls_back_to_its_tail() -> None:
    log = "\n".join(f"line {i}" for i in range(500))
    excerpt = _log_excerpt(log)
    assert excerpt.endswith("line 499") and "line 100" not in excerpt
    assert len(_log_excerpt("FAILED x\n" * 2000)) <= 3000


async def test_the_same_red_head_is_not_sent_back_twice() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf, head_sha=_HEAD, ci_red_head_sha=_HEAD, ci_fix_attempts=1, ci_fix_head_sha=_HEAD
        )
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_red_check())
        await _worker(sf, github, rec).drain_once()

        assert rec.ci == [] and rec.stalls == []
        assert (await _row(sf, row)).status is MergeWatchStatus.PENDING_CI


async def test_past_the_resend_limit_the_founder_gets_one_decision_with_the_checks() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, head_sha=_HEAD, ci_red_head_sha=_HEAD, ci_fix_attempts=2)
        rec = _Recorder()
        github = _Github(pr=_open_pr("unstable"), checks=_red_check())
        await _worker(sf, github, rec).drain_once()

        assert rec.ci == []
        [stall] = rec.stalls
        assert stall["reason"] == "ci_failed"
        assert [c["name"] for c in stall["failed_checks"]] == ["lint-and-test"]
        assert (await _row(sf, row)).status is MergeWatchStatus.FAILED


async def test_sending_red_ci_back_reopens_the_run_with_the_checks_in_its_payload() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, run_status=RunStatus.REVIEW_READY)
        checks = [{"name": "lint-and-test", "conclusion": "failure", "summary": "boom"}]

        with workspace_scope(row.workspace_id):  # the worker publishes this per watched PR
            await build_merge_watch_ci_redispatch(session_factory=sf)(
                row.run_id, failed_checks=checks, pr_number=1222, head_sha=_HEAD
            )

        run = await _run(sf, row)
        assert run.status is RunStatus.OPEN
        assert run.payload["ci_failure"]["failed_checks"] == checks
        [message] = ci_failure_messages(run)
        assert "lint-and-test" in message["content"] and "boom" in message["content"]
        assert "SAME branch" in message["content"]
        consume_ci_failure(run)
        assert ci_failure_messages(run) == []


async def test_a_shipped_run_is_not_revived_by_red_ci() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(sf, run_status=RunStatus.SHIPPED)
        async with sf() as session:
            async with workspace_session_scope(session, row.workspace_id):
                run = await session.get(ExecutionRun, row.run_id)
                assert run is not None
                sent = await send_ci_failure_to_run(
                    session, run, failed_checks=[], pr_number=1222, head_sha=None
                )
        assert sent is False


async def test_the_founders_fix_again_revives_a_failed_watch() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf,
            status=MergeWatchStatus.FAILED,
            run_status=RunStatus.REVIEW_READY,
            ci_fix_attempts=2,
            ci_fix_head_sha=_HEAD,
            ci_red_head_sha=_HEAD,
        )
        async with sf() as session:
            async with workspace_session_scope(session, row.workspace_id):
                run = await session.get(ExecutionRun, row.run_id)
                assert run is not None
                await revive_merge_watch(session, run, pr_number=1222, ci_deadline_s=_CI_DEADLINE_S)
            await session.commit()

        fetched = await _row(sf, row)
        assert fetched.status is MergeWatchStatus.PENDING_CI
        assert fetched.ci_fix_attempts == 0
        assert fetched.ci_red_head_sha is None


# ── 3. a failed watch still sees its PR merge or close ──────────────────────────────────


async def test_a_failed_watchs_pr_merged_by_hand_ships_the_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf,
            status=MergeWatchStatus.FAILED,
            run_status=RunStatus.REVIEW_READY,
            run_payload={"intent_text": "x", AWAITING_MERGE_KEY: True},
        )
        merged = {"state": "closed", "merged": True, "mergeable_state": "unknown"}
        worker = _worker(
            sf,
            _Github(pr=merged),
            _Recorder(),
            pr_concluded=build_merge_watch_pr_concluded(session_factory=sf),
        )
        await worker.drain_once()

        run = await _run(sf, row)
        assert run.status is RunStatus.SHIPPED
        assert AWAITING_MERGE_KEY not in (run.payload or {})
        assert (await _row(sf, row)).status is MergeWatchStatus.MERGED


async def test_a_failed_watchs_pr_closed_cancels_the_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf, status=MergeWatchStatus.FAILED, run_status=RunStatus.REVIEW_READY
        )
        closed = {"state": "closed", "merged": False, "mergeable_state": "dirty"}
        worker = _worker(
            sf,
            _Github(pr=closed),
            _Recorder(),
            pr_concluded=build_merge_watch_pr_concluded(session_factory=sf),
        )
        await worker.drain_once()

        assert (await _run(sf, row)).status is RunStatus.CANCELLED


async def test_a_failed_watch_with_an_open_pr_just_looks_again_later_and_says_nothing() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        row = await _seed_watch(
            sf, status=MergeWatchStatus.FAILED, run_status=RunStatus.REVIEW_READY
        )
        rec = _Recorder()
        worker = _worker(sf, _Github(pr=_open_pr("clean")), rec)
        await worker.drain_once()

        fetched = await _row(sf, row)
        assert fetched.status is MergeWatchStatus.FAILED, "a failed watch never merges on its own"
        assert _at(fetched.next_poll_at) > _NOW + timedelta(minutes=5)
        assert rec.stalls == [] and rec.ci == []
        assert (await _run(sf, row)).status is RunStatus.REVIEW_READY
