"""A pull request whose checks FAILED is not a pull request that ran out of time.

The state machine routes ``clean`` to the merge and ``behind``/``dirty`` to the
freshness merge. Everything else falls into one bucket commented "CI still
pending (blocked / unstable / unknown / anything else)" and waits out
``github_auto_merge_ci_deadline_s`` — an hour by default — before reporting
``ci_deadline_exceeded``.

``unstable`` is not pending. It is GitHub saying the checks came back and at
least one of them failed. Live (PR #754, 2026-08-15): ``lint-and-test`` failed
**29 seconds** after the PR opened, and the founder was told 63 minutes later
that "the checks never went green in time" — late, and about the wrong thing.
A failing test and a hung CI call for different actions from the person reading
the phone.

So a decided failure is reported as a failure, when it is decided.

2026-09-08 — "decided" was too strong, and it cost a merge. PR #892's
``lint-and-test`` went red at 07:00, the watch called it decided, wrote the row
FAILED and stopped; a re-run of the SAME commit was green at 07:25 and nobody
was watching, so the PR sat open until I merged it by hand. ``failed`` is not in
``_CLAIMABLE_STATUSES``, so going terminal is what closes the eye.

A red check is decided about THIS attempt, not about the commit. So the watch
now looks twice at the same head before calling the founder, and never stops
watching in between — which is all it takes, because GitHub's check-runs
endpoint already answers with the LATEST attempt per check name. Measured on
that very commit: ``filter=latest`` (the default the client uses) returns
lint-and-test SUCCESS, and only ``filter=all`` still shows the old failure. So a
re-run turns the PR ``clean`` and the existing merge step takes it — no
per-attempt bookkeeping needed anywhere.

The founder still hears "the checks failed" rather than "they never went green
in time", now at most one poll interval later — not the 63 minutes #754 was
about.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def test_failed_checks_are_reported_as_failed_not_as_a_deadline() -> None:
    """The founder must be told what actually happened. ``ci_deadline_exceeded``
    on a run whose checks came back red is a false statement about a real
    event."""
    worker, repo, escalations = _worker()
    snap = _snap(deadline_at=_NOW + timedelta(hours=1))  # nowhere near the deadline

    await worker._process(snap, _NOW)  # first look — red, but re-runs happen
    await worker._process(_seen_red(snap), _NOW)

    assert repo.marked, "the row must reach a terminal, not keep polling"
    _row_id, status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") == "ci_failed"
    assert escalations and escalations[-1][1] == "ci_failed", "and the founder is told"


async def test_it_does_not_wait_out_the_deadline_first() -> None:
    """The whole cost of the old behaviour was the WAIT. GitHub had the answer
    at 29 seconds; the founder heard at 63 minutes. One extra poll interval is
    not that wait — an hour of silence is."""
    worker, repo, _escalations = _worker()
    snap = _snap(deadline_at=_NOW + timedelta(hours=1))

    await worker._process(snap, _NOW)
    await worker._process(_seen_red(snap), _NOW)

    _row_id, status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") != "ci_deadline_exceeded"
    assert str(status).endswith("FAILED") or "failed" in str(status).lower()


# ── the second look ──────────────────────────────────────────────────────────


async def test_the_first_red_keeps_watching_and_says_nothing_yet() -> None:
    """The defect PR #892 hit. A first red must leave the row CLAIMABLE — the
    terminal is what stopped anyone from ever seeing the re-run go green — and
    must not spend the founder's attention on a check that may well be re-run."""
    worker, repo, escalations = _worker()

    await worker._process(_snap(deadline_at=_NOW + timedelta(hours=1)), _NOW)

    _row_id, status, kwargs = repo.marked[-1]
    assert "pending_ci" in str(status).lower(), (
        "a terminal row is not claimable, and an unclaimable row is never looked at again"
    )
    assert kwargs.get("ci_red_head_sha") == "deadbeef", "remember WHICH head was red"
    assert escalations == [], "a first red is not yet worth the founder's attention"


async def test_a_re_run_that_goes_green_merges_and_never_calls_the_founder() -> None:
    """The whole point. Same commit, same watch row — the re-run turns the PR
    ``clean`` and it merges, with nothing said to anyone."""
    worker, repo, escalations = _worker()
    snap = _snap(deadline_at=_NOW + timedelta(hours=1))

    await worker._process(snap, _NOW)

    # GitHub replaces the check's conclusion on a re-run (measured), so the PR
    # goes clean. This is the poll that used to never happen.
    worker._client.set_green()
    await worker._process(_seen_red(snap), _NOW)

    assert escalations == [], "a PR that merged is not a PR the founder must look at"
    assert worker._client.merged, "the re-run's green must actually be merged"


async def test_a_new_head_starts_the_grace_over() -> None:
    """The agent re-pushed, so the red we remember was about code that no longer
    exists. Counting it toward the new head would call the founder on the FIRST
    red of a commit nobody has re-run yet."""
    worker, repo, escalations = _worker()
    snap = _snap(deadline_at=_NOW + timedelta(hours=1))

    await worker._process(snap, _NOW)
    worker._client.set_head("cafebabe")  # the agent pushed a fix; still red
    await worker._process(_seen_red(snap), _NOW)

    _row_id, status, kwargs = repo.marked[-1]
    assert "pending_ci" in str(status).lower()
    assert kwargs.get("ci_red_head_sha") == "cafebabe", "the grace follows the head"
    assert escalations == []


async def test_checks_still_running_keep_waiting() -> None:
    """The distinction has to cut the right way: a PR whose checks have not come
    back yet is exactly what the deadline is for, and must NOT be called a
    failure."""
    worker, repo, escalations = _worker(mergeable_state="blocked", conclusion=None)
    snap = _snap(deadline_at=_NOW + timedelta(hours=1))

    await worker._process(snap, _NOW)

    _row_id, _status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") == "ci_pending"
    assert escalations == [], "nothing decided yet — nothing to tell the founder"


async def test_a_genuinely_hung_ci_still_reports_the_deadline() -> None:
    """Unchanged behaviour for the case the deadline was written for."""
    worker, repo, escalations = _worker(mergeable_state="blocked", conclusion=None)
    snap = _snap(deadline_at=_NOW - timedelta(minutes=1))  # past it

    await worker._process(snap, _NOW)

    _row_id, _status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") == "ci_deadline_exceeded"
    assert escalations and escalations[-1][1] == "ci_deadline_exceeded"


# ── the minimum of the worker these tests drive ──────────────────────────────


def _snap(*, deadline_at: datetime) -> Any:
    from backend.workflow.infrastructure.workers.merge_watch_worker import _WatchSnapshot

    return _WatchSnapshot(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        repo="acme/app",
        pr_number=754,
        branch="run/abc",
        base_branch="main",
        attempts=0,
        deadline_at=deadline_at,
        conflict_dispatched=False,
        conflict_head_sha=None,
        conflict_attempts=0,
        conflict_dispatched_at=None,
        ci_red_head_sha=None,
    )


def _seen_red(snap: Any, head: str = "deadbeef") -> Any:
    """The same watch row as ``snap``, re-read on the NEXT poll pass.

    The worker snapshots the row before its claim transaction commits, so a
    second ``_process`` in production reads whatever the first one wrote. These
    tests stub the repository, so the carry-over is spelled out here instead of
    being silently supplied — a snapshot that kept its own ``None`` would let a
    broken write pass."""
    import dataclasses

    return dataclasses.replace(snap, ci_red_head_sha=head)


class _Repo:
    def __init__(self) -> None:
        self.marked: list[tuple[Any, Any, dict[str, Any]]] = []

    async def mark_status(self, row_id: Any, status: Any, **kwargs: Any) -> None:
        self.marked.append((row_id, status, kwargs))


class _Client:
    def __init__(self, mergeable_state: str, conclusion: str | None = None) -> None:
        self._state = mergeable_state
        self._conclusion = conclusion
        self._head = "deadbeef"
        self.merged = False

    def set_green(self) -> None:
        """What a re-run of the same commit actually does: the check-runs
        endpoint reports the LATEST attempt, so the red is simply gone and the
        PR becomes mergeable."""
        self._conclusion = "success"
        self._state = "clean"

    def set_head(self, sha: str) -> None:
        self._head = sha

    async def merge_pr(self, *_a: Any, **_k: Any) -> Any:
        from plugin.github.client import MergeResult

        self.merged = True
        return MergeResult(status="merged", merged=True, sha=self._head)

    async def get_check_runs(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        if self._conclusion is None:
            # Still running — the pending case the deadline exists for.
            return [{"name": "lint-and-test", "status": "in_progress", "conclusion": None}]
        return [
            {"name": "lint-and-test", "status": "completed", "conclusion": self._conclusion},
            {"name": "pwa", "status": "completed", "conclusion": "success"},
        ]

    async def get_pr(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "merged": False,
            "state": "open",
            "mergeable_state": self._state,
            "head": {"sha": self._head},
        }


def _worker(
    *, mergeable_state: str = "unstable", conclusion: str | None = "failure"
) -> tuple[Any, _Repo, list[Any]]:
    from contextlib import asynccontextmanager

    from backend.workflow.infrastructure.workers import merge_watch_worker as mod

    repo = _Repo()
    escalations: list[Any] = []

    async def _escalate(run_id: uuid.UUID, *, reason: str, repo_: str = "", **_k: Any) -> None:
        escalations.append((run_id, reason))

    class _Session:
        #: The merge step takes a per-repo advisory lock, which asks the bound
        #: dialect whether it is Postgres. No bind means it is not, and the lock
        #: degrades to a no-op exactly as it does on SQLite.
        bind = None

        async def commit(self) -> None:
            return None

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    @asynccontextmanager
    async def _factory() -> Any:
        yield _Session()

    client = _Client(mergeable_state, conclusion)
    worker = mod.MergeWatchWorker(
        session_factory=lambda: _Session(),  # type: ignore[arg-type]
        client_resolver=_returns(client),
    )
    worker._client = client  # type: ignore[attr-defined] — the tests flip its answers
    worker._gave_up = lambda snap, reason: _escalate(snap.run_id, reason=reason)  # type: ignore[assignment]
    worker._run_cancelled = _returns(False)  # type: ignore[method-assign]
    mod.GithubMergeWatchRepository = lambda _s: repo  # type: ignore[assignment]
    return worker, repo, escalations


def _returns(value: Any) -> Any:
    async def _f(*_a: Any, **_k: Any) -> Any:
        return value

    return _f


async def test_an_unreadable_checks_api_is_not_a_verdict() -> None:
    """Fail-SOFT. Telling the founder "your CI failed" because GitHub returned a
    502 is a worse untruth than the one this fixes — and it would send them to
    look at a test suite that is perfectly fine. An unreadable API keeps today's
    behaviour: keep polling until the deadline."""
    worker, repo, escalations = _worker()

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("502 Bad Gateway")

    client = await worker._resolve_client(None, uuid.uuid4(), uuid.uuid4())
    client.get_check_runs = _boom  # type: ignore[assignment]

    await worker._process(_snap(deadline_at=_NOW + timedelta(hours=1)), _NOW)

    _row_id, _status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") == "ci_pending"
    assert escalations == []


async def test_a_check_still_running_alongside_a_red_one_is_still_a_failure() -> None:
    """A red required check does not become green because a sibling is still
    going. Waiting for the rest to finish only delays the same answer."""
    worker, repo, escalations = _worker()
    client = await worker._resolve_client(None, uuid.uuid4(), uuid.uuid4())

    async def _mixed(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [
            {"name": "lint-and-test", "status": "completed", "conclusion": "failure"},
            {"name": "e2e", "status": "in_progress", "conclusion": None},
        ]

    client.get_check_runs = _mixed  # type: ignore[assignment]

    snap = _snap(deadline_at=_NOW + timedelta(hours=1))
    await worker._process(snap, _NOW)
    await worker._process(_seen_red(snap), _NOW)

    _row_id, _status, kwargs = repo.marked[-1]
    assert kwargs.get("last_error") == "ci_failed"
    assert escalations and escalations[-1][1] == "ci_failed"


async def test_the_founder_hears_failed_not_timed_out() -> None:
    """The two reasons must not collapse into one sentence — they send the
    founder to different places."""
    from types import SimpleNamespace

    from backend.notifications.copy import needs_you_reason_body
    from backend.workflow.application._checkpoint_shared import _question_text

    for lang in ("en", "ko"):
        failed = needs_you_reason_body("ci_failed", lang)
        timeout = needs_you_reason_body("ci_deadline_exceeded", lang)
        generic = needs_you_reason_body("not-in-the-catalog", lang)
        assert failed not in (timeout, generic), f"{lang}: ci_failed needs its own body"

        line = _question_text(
            SimpleNamespace(decision="merge_watch_stalled", payload={"reason": "ci_failed"}),  # type: ignore[arg-type]
            lang,
        )
        assert line and "ci_failed" not in line
