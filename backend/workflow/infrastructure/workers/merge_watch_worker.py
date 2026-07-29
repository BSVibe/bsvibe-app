"""MergeWatchWorker — CI-green auto-merge poller (Phase 1).

PR4 of the auto-merge feature. Drains the ``github_merge_watch`` durable queue
(one row per opened PR under watch) and, per row, runs a small state machine
against the GitHub REST API: wait until the PR's checks are green + it is
``clean``-mergeable, then squash-merge it under a per-repo advisory lock so two
PRs on the SAME ``owner/name`` can never merge concurrently.

Model (EXACTLY on :class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`)
----------------------------------------------------------------------------------------------------

* Subclass of :class:`~backend.workers.base.BaseWorker`; overrides ``_tick`` →
  :meth:`drain_once`; a ``*WorkerConfig`` dataclass carries the tunables.
* :func:`~backend.workflow.infrastructure.github.repository.build_merge_watch_claim_stmt`
  supplies the multi-server-safe ``FOR UPDATE SKIP LOCKED`` claim (via
  ``GithubMergeWatchRepository.claim_due``) — a second poller on the same DB
  skips the rows this one claimed.

Transaction discipline
----------------------

Unlike the delivery worker (which holds one transaction across the whole batch),
this worker MUST NOT hold a transaction open across the network calls to GitHub.
So the claim pass (one short transaction) *reserves* each claimed row by pushing
its ``next_poll_at`` forward, then commits + releases the claim lock; each row is
then processed in its OWN fresh session/transaction that opens only around the
GitHub calls + the single status transition. A crash mid-process leaves the row
reserved (retried after the reservation window) — at-least-once + restart-safe.

At-least-once / restart-safe
----------------------------

All progress lives on the row (``status`` / ``attempts`` / ``next_poll_at`` /
``deadline_at``). A crash between the GitHub merge call and the ``merged``
transition is safe: the next claim re-reads ``get_pr`` and an already-merged PR
maps straight to ``merged`` (idempotent), so we never double-merge.

Deferred to later PRs
---------------------

* ``behind`` / ``dirty`` PRs (a PR needing a rebase / carrying conflicts) are
  Phase 2 (PR6 — freshness / conflict recovery). PR4 does NOT merge them: the
  row simply waits (``pending_ci`` + backoff, ``last_error="awaiting_freshness
  (pr6)"``).
* The CI-deadline founder Decision (``human_review_required``) is deferred to
  PR7: on deadline the row is FAILED + a prominent ``merge_watch_ci_deadline``
  warning is logged, but the worker does not reach across into the application
  ``create_decision`` seam from the infrastructure layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.storage.github_repo_lock import GithubRepoBusy, github_repo_lock
from backend.workers.base import BaseWorker
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.github.repository import GithubMergeWatchRepository
from plugin.github.client import MergeResult

logger = structlog.get_logger(__name__)


class MergeWatchClient(Protocol):
    """The narrow GitHub surface the state machine consumes.

    Structurally satisfied by :class:`plugin.github.client.GithubClient`; a test
    injects a fake. Kept a Protocol (port defined where used) so the worker
    depends on the interface, not the concrete client.
    """

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]: ...

    async def merge_pr(
        self, owner: str, repo: str, number: int, *, method: str = "squash"
    ) -> MergeResult: ...


#: Resolve a per-row GitHub client (token + base_url from the workspace's github
#: binding). Returns ``None`` when the workspace has no resolvable github
#: delivery target (the connector was removed / deactivated). Injected so the
#: worker (infrastructure) never imports the application-layer binding resolver.
MergeClientResolver = Callable[[AsyncSession, uuid.UUID], Awaitable[MergeWatchClient | None]]


@dataclass(slots=True, frozen=True)
class FreshnessTarget:
    """The git-side facts the freshness merge needs, resolved from the
    workspace's github binding: the ``repo`` (``owner/name``), its ``base_branch``,
    the decrypted push/fetch ``token`` (``None`` for a local ``file://`` remote in
    tests), and the ``remote_url`` used to RE-CLONE a reaped run workspace.

    Resolved in the APPLICATION layer (binding + token decryption is an
    application concern) and injected, so the infrastructure worker never imports
    the binding resolver / cipher.
    """

    repo: str
    base_branch: str
    token: str | None
    remote_url: str


#: Resolve the git-side freshness target for a workspace (binding + decrypted
#: token + clone URL). ``None`` when the workspace has no resolvable github
#: delivery target. Injected — the worker never decrypts a credential itself.
FreshnessResolver = Callable[[AsyncSession, uuid.UUID], Awaitable["FreshnessTarget | None"]]


class ConflictRedispatch(Protocol):
    """Re-dispatch a run to the agent to resolve a merge conflict (PR7's side).

    Implemented in the APPLICATION layer (``merge_watch_runtime``): it writes
    ``run.payload["merge_conflict"]`` and transitions the run RUNNING → OPEN via
    ``AgentRunner.transition`` — both application concerns — so the infrastructure
    worker just calls it. Injected as a callable so the worker never imports the
    AgentRunner / run repositories.
    """

    async def __call__(
        self,
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None: ...


#: A clock — injected so tests pin ``now`` (deadline / backoff are time-driven).
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _split_repo(repo: str) -> tuple[str, str]:
    """``"owner/name"`` → ``("owner", "name")`` (first slash wins)."""
    owner, _, name = repo.partition("/")
    return owner, name


@dataclass(slots=True)
class _WatchSnapshot:
    """The fields of a claimed row the per-row processing needs, captured before
    the claim transaction commits (so the row objects can expire)."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    deliverable_id: uuid.UUID
    repo: str
    pr_number: int
    branch: str
    base_branch: str
    attempts: int
    deadline_at: datetime
    conflict_dispatched: bool

    @classmethod
    def of(cls, row: GithubMergeWatchRow) -> _WatchSnapshot:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            run_id=row.run_id,
            deliverable_id=row.deliverable_id,
            repo=row.repo,
            pr_number=row.pr_number,
            branch=row.branch,
            base_branch=row.base_branch,
            attempts=row.attempts,
            deadline_at=row.deadline_at,
            conflict_dispatched=row.conflict_dispatched,
        )


@dataclass(slots=True)
class MergeWatchWorkerConfig:
    batch_size: int = 20
    #: The BaseWorker idle-loop cadence between claim passes.
    poll_interval_s: float = 30.0
    #: Per-row poll backoff: ``min(base * 2**attempts, cap)``.
    backoff_base_s: float = 30.0
    backoff_cap_s: float = 300.0


class MergeWatchWorker(BaseWorker):
    """Periodic CI-green auto-merge poll over ``github_merge_watch``."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client_resolver: MergeClientResolver,
        freshness_resolver: FreshnessResolver | None = None,
        redispatch_conflict: ConflictRedispatch | None = None,
        git_ops: GitOps | None = None,
        run_workspace_root: Path | None = None,
        config: MergeWatchWorkerConfig | None = None,
        now: Clock | None = None,
    ) -> None:
        self._cfg = config or MergeWatchWorkerConfig()
        super().__init__(name="merge_watch_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._resolve_client = client_resolver
        # PR6 — the behind/dirty freshness merge deps. All three are wired
        # together by the runtime; when any is absent (e.g. a PR4-era worker
        # construction) the behind/dirty branch falls back to the old "wait"
        # behavior rather than attempting a merge it cannot complete.
        self._resolve_freshness = freshness_resolver
        self._redispatch_conflict = redispatch_conflict
        self._git = git_ops or GitOps()
        self._run_workspace_root = run_workspace_root
        self._now = now or _utcnow

    @property
    def _freshness_wired(self) -> bool:
        return (
            self._resolve_freshness is not None
            and self._redispatch_conflict is not None
            and self._run_workspace_root is not None
        )

    async def _tick(self) -> int:
        return await self.drain_once()

    def _backoff(self, attempts: int, now: datetime) -> datetime:
        delay = min(self._cfg.backoff_base_s * (2**attempts), self._cfg.backoff_cap_s)
        return now + timedelta(seconds=delay)

    async def drain_once(self) -> int:
        """Claim a batch of due rows, reserve them, then process each in its own
        short transaction. Returns the count of rows processed."""
        now = self._now()
        async with self._session_factory() as session:
            repo = GithubMergeWatchRepository(session)
            rows = await repo.claim_due(now=now, batch_size=self._cfg.batch_size)
            if not rows:
                return 0
            snapshots = [_WatchSnapshot.of(r) for r in rows]
            # Reserve each claimed row: push next_poll_at forward so a sibling
            # poller won't re-claim it while we process it in a separate txn.
            # A per-row status transition below overrides this reservation; a
            # crash before that leaves the reservation standing (retry later).
            reserve_at = now + timedelta(seconds=self._cfg.poll_interval_s)
            for row in rows:
                row.next_poll_at = reserve_at
            await session.commit()

        processed = 0
        for snap in snapshots:
            try:
                await self._process(snap, now)
            except GithubRepoBusy:
                # Lost the per-repo merge lock to a sibling — leave the row
                # reserved and retry next tick (mirror the ProductWorkspaceBusy
                # contract: never block on the lock).
                logger.info(
                    "merge_watch_repo_busy",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                )
            except Exception:  # noqa: BLE001 — one bad row must not kill the tick
                logger.exception(
                    "merge_watch_process_failed",
                    row_id=str(snap.id),
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                )
            processed += 1
        return processed

    async def _process(self, snap: _WatchSnapshot, now: datetime) -> None:
        """Run the state machine for one watched PR in its own transaction."""
        owner, name = _split_repo(snap.repo)
        async with self._session_factory() as session:
            repo = GithubMergeWatchRepository(session)
            client = await self._resolve_client(session, snap.workspace_id)
            if client is None:
                # No resolvable github target (connector removed / deactivated) —
                # we can never make progress, so stop watching.
                await repo.mark_status(
                    snap.id,
                    MergeWatchStatus.FAILED,
                    last_error="github_binding_unavailable",
                )
                await session.commit()
                logger.warning(
                    "merge_watch_no_client",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    workspace_id=str(snap.workspace_id),
                )
                return

            pr = await client.get_pr(owner, name, snap.pr_number)
            merged = pr.get("merged")
            state = pr.get("state")
            mergeable_state = pr.get("mergeable_state")

            # 1. Idempotent terminals — the PR already resolved out from under us.
            if merged is True:
                await repo.mark_status(snap.id, MergeWatchStatus.MERGED)
                await session.commit()
                logger.info("merge_watch_already_merged", repo=snap.repo, pr_number=snap.pr_number)
                return
            if state == "closed":
                await repo.mark_status(
                    snap.id, MergeWatchStatus.ABANDONED, last_error="pr_closed_unmerged"
                )
                await session.commit()
                logger.info("merge_watch_abandoned", repo=snap.repo, pr_number=snap.pr_number)
                return

            # 2. Mergeability gate.
            if mergeable_state == "clean":
                await self._merge_step(session, repo, snap, owner, name, client, now)
                await session.commit()
                return

            if mergeable_state in ("behind", "dirty"):
                # PR6 — an AUTHORITATIVE local freshness merge: don't trust
                # GitHub's behind/dirty label, do the merge under the per-repo
                # lock to decide clean-vs-conflict for real (serialized with the
                # merge step so PR#2 always freshens against PR#1's merged main).
                await self._freshness_step(session, repo, snap, now)
                await session.commit()
                return

            # 3. CI still pending (blocked / unstable / unknown / anything else).
            if now > snap.deadline_at:
                await repo.mark_status(
                    snap.id, MergeWatchStatus.FAILED, last_error="ci_deadline_exceeded"
                )
                await session.commit()
                logger.warning(
                    "merge_watch_ci_deadline",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    run_id=str(snap.run_id),
                    deliverable_id=str(snap.deliverable_id),
                )
                # TODO(PR7): raise a ``human_review_required`` Decision on the run
                # so the founder is told the PR's CI never went green. Deferred to
                # keep the worker in the infrastructure layer — routing a Decision
                # (which also emits the founder notification) belongs to the
                # application ``create_decision`` seam, not a cross-layer call here.
                return

            await repo.mark_status(
                snap.id,
                MergeWatchStatus.PENDING_CI,
                next_poll_at=self._backoff(snap.attempts, now),
                last_error="ci_pending",
                increment_attempt=True,
            )
            await session.commit()

    async def _merge_step(
        self,
        session: AsyncSession,
        repo: GithubMergeWatchRepository,
        snap: _WatchSnapshot,
        owner: str,
        name: str,
        client: MergeWatchClient,
        now: datetime,
    ) -> None:
        """Squash-merge under the per-repo advisory lock (mergeable_state=clean).

        Raises :class:`GithubRepoBusy` on the loser path (caught by
        :meth:`drain_once`). Re-confirms ``clean`` under the lock to guard the
        race where the state changed between the pre-lock read and the merge.
        """
        async with github_repo_lock(session, snap.repo):
            confirm = await client.get_pr(owner, name, snap.pr_number)
            if confirm.get("mergeable_state") != "clean":
                # Changed under us before we took the lock — back to pending_ci.
                await repo.mark_status(
                    snap.id,
                    MergeWatchStatus.PENDING_CI,
                    next_poll_at=self._backoff(snap.attempts, now),
                    last_error="mergeable_state_changed",
                    increment_attempt=True,
                )
                return
            result = await client.merge_pr(owner, name, snap.pr_number, method="squash")
            if result.merged:
                await repo.mark_status(snap.id, MergeWatchStatus.MERGED)
                logger.info(
                    "merge_watch_merged",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    sha=result.sha,
                )
                return
            # not_mergeable (405) / head_changed (409) — the head moved or the PR
            # is momentarily non-mergeable. Back to pending_ci with backoff; a
            # later tick re-reads the fresh head (at-least-once, no crash).
            await repo.mark_status(
                snap.id,
                MergeWatchStatus.PENDING_CI,
                next_poll_at=self._backoff(snap.attempts, now),
                last_error=result.status,
                increment_attempt=True,
            )

    async def _freshness_step(
        self,
        session: AsyncSession,
        repo: GithubMergeWatchRepository,
        snap: _WatchSnapshot,
        now: datetime,
    ) -> None:
        """behind/dirty → an authoritative LOCAL freshness merge (PR6).

        Merge ``origin/<base_branch>`` INTO the PR branch locally to decide, for
        real, whether the branch is cleanly freshenable or genuinely conflicts:

        * clean  → push the freshened branch (CI re-runs on the new head; a later
          clean poll merges) → back to ``pending_ci`` (backoff).
        * conflict → hand the run off to the agent: re-dispatch (once) + park the
          row in ``needs_resolution`` until the agent re-pushes a resolved head.

        Runs under the per-repo :func:`github_repo_lock` (serialized WITH the
        merge step). A lost lock raises :class:`GithubRepoBusy` — propagated to
        :meth:`drain_once` (retried next tick) with NO partial state. A git
        failure never crashes the worker: it logs + backs off to ``pending_ci``.
        """
        # Un-wired (PR4-era construction) — the freshness merge deps aren't
        # injected, so we cannot make progress on a behind/dirty PR. Preserve the
        # old "wait" behavior rather than half-attempt a merge.
        if not self._freshness_wired:
            await repo.mark_status(
                snap.id,
                MergeWatchStatus.PENDING_CI,
                next_poll_at=self._backoff(snap.attempts, now),
                last_error="awaiting_freshness_unwired",
                increment_attempt=True,
            )
            return

        # Loop guard — a conflict was already handed to the agent. Do NOT re-merge
        # / re-dispatch: wait (backoff) for the agent's re-push to move the head (a
        # later clean poll merges it). At most one dispatch per detected conflict.
        if snap.conflict_dispatched:
            await repo.mark_status(
                snap.id,
                MergeWatchStatus.NEEDS_RESOLUTION,
                next_poll_at=self._backoff(snap.attempts, now),
                last_error="awaiting_conflict_resolution",
                increment_attempt=True,
            )
            return

        # narrow Optionals for mypy (guarded by ``_freshness_wired`` above).
        assert self._resolve_freshness is not None  # noqa: S101
        assert self._redispatch_conflict is not None  # noqa: S101
        assert self._run_workspace_root is not None  # noqa: S101

        async with github_repo_lock(session, snap.repo):
            target = await self._resolve_freshness(session, snap.workspace_id)
            if target is None:
                # No resolvable github target (connector removed) — can't freshen.
                await repo.mark_status(
                    snap.id,
                    MergeWatchStatus.FAILED,
                    last_error="github_binding_unavailable",
                )
                logger.warning(
                    "merge_watch_freshen_no_target",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    workspace_id=str(snap.workspace_id),
                )
                return

            clone = self._run_workspace_root / str(snap.run_id)
            try:
                await self._ensure_clone(clone, snap, target)
                await self._git.fetch(
                    clone, "origin", target.base_branch, token=target.token, unshallow=True
                )
                result = await self._git.merge_ref(clone, f"origin/{target.base_branch}")
            except GitError:
                logger.warning(
                    "merge_watch_freshen_failed",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    run_id=str(snap.run_id),
                    exc_info=True,
                )
                await repo.mark_status(
                    snap.id,
                    MergeWatchStatus.PENDING_CI,
                    next_poll_at=self._backoff(snap.attempts, now),
                    last_error="freshen_failed",
                    increment_attempt=True,
                )
                return

            if result.status == "clean":
                try:
                    await self._git.push(clone, snap.branch, token=target.token)
                except GitError:
                    logger.warning(
                        "merge_watch_freshen_failed",
                        repo=snap.repo,
                        pr_number=snap.pr_number,
                        run_id=str(snap.run_id),
                        exc_info=True,
                    )
                    await repo.mark_status(
                        snap.id,
                        MergeWatchStatus.PENDING_CI,
                        next_poll_at=self._backoff(snap.attempts, now),
                        last_error="freshen_failed",
                        increment_attempt=True,
                    )
                    return
                await repo.mark_status(
                    snap.id,
                    MergeWatchStatus.PENDING_CI,
                    next_poll_at=self._backoff(snap.attempts, now),
                    last_error="freshened",
                    increment_attempt=True,
                )
                logger.info(
                    "merge_watch_freshened",
                    repo=snap.repo,
                    pr_number=snap.pr_number,
                    run_id=str(snap.run_id),
                    branch=snap.branch,
                )
                return

            # Conflict — hand the run off to the agent to resolve. Re-dispatch
            # exactly once (guarded above by ``conflict_dispatched``), then park
            # the row in ``needs_resolution`` awaiting the agent's re-push.
            await self._redispatch_conflict(
                snap.run_id,
                conflict_paths=list(result.conflict_paths),
                base_branch=target.base_branch,
                pr_number=snap.pr_number,
            )
            logger.info(
                "merge_watch_conflict_dispatched",
                repo=snap.repo,
                pr_number=snap.pr_number,
                run_id=str(snap.run_id),
                conflict_paths=result.conflict_paths,
            )
            await repo.mark_status(
                snap.id,
                MergeWatchStatus.NEEDS_RESOLUTION,
                next_poll_at=self._backoff(snap.attempts, now),
                last_error="merge_conflict",
                increment_attempt=True,
                conflict_dispatched=True,
            )

    async def _ensure_clone(
        self, clone: Path, snap: _WatchSnapshot, target: FreshnessTarget
    ) -> None:
        """Ensure the run's clone exists at ``clone`` on the PR branch.

        Present (``.git`` dir) → reuse it (a shallow clone is fine — the caller
        unshallows in the fetch step). MISSING (run cleanup reaped the workspace
        after the PR opened) → RE-CLONE fresh at FULL depth (so a merge base with
        ``base_branch`` exists) and check out the PR branch tracking
        ``origin/<branch>``.
        """
        if (clone / ".git").exists():
            return
        logger.info(
            "merge_watch_reclone",
            repo=snap.repo,
            pr_number=snap.pr_number,
            run_id=str(snap.run_id),
            branch=snap.branch,
        )
        clone.parent.mkdir(parents=True, exist_ok=True)
        # depth=0 → a FULL clone: a shallow re-clone would lack the merge base.
        await self._git.clone(target.remote_url, clone, token=target.token, depth=0)
        await self._git.checkout(clone, snap.branch)


__all__ = [
    "Clock",
    "ConflictRedispatch",
    "FreshnessResolver",
    "FreshnessTarget",
    "MergeClientResolver",
    "MergeWatchClient",
    "MergeWatchWorker",
    "MergeWatchWorkerConfig",
]
