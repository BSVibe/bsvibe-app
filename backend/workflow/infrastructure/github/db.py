"""``github_merge_watch`` persistence schema — the auto-merge poll queue.

PR3 of the auto-merge feature. One row per opened PR eligible for CI-green
auto-merge. Mirrors the delivery-table pattern
(:class:`~backend.workflow.infrastructure.delivery.db.DeliveryEventRow`):

* Registers on the single shared :class:`~backend.data.Base` metadata (the
  same ``Base`` the delivery table's ``DeliveryBase`` aliases), so the
  migrations already manage it — no separate autogenerate target.
* No cross-``Base`` FK at the SQLAlchemy layer — ``run_id`` / ``deliverable_id``
  are loose UUIDs here exactly as the delivery table leaves ``deliverable_id``
  loose; the delivery table adds NO raw migration-layer FK either, so this
  table follows the same convention (integrity is not FK-enforced at the DB).
* ``status`` is a Postgres native enum (``SAEnum`` with ``values_callable``),
  matching the ``DeliveryStatus``/``SafeModeStatus`` prevailing choice.

The table is drained by a later CI-green auto-merge poller worker; there is no
worker consuming it yet, so it is intentionally NOT declared as a Channel (a
Channel must name at least one consumer — INV-1).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

# Same ``Base`` the delivery module aliases as ``DeliveryBase`` — one shared
# metadata so the migrations manage this table alongside the rest.
GithubMergeWatchBase = Base


class MergeWatchStatus(StrEnum):
    """Lifecycle of one watched PR.

    ``pending_ci`` — opened, waiting for the head SHA's checks to go green.
    ``merging`` — checks green, a merge attempt is in flight.
    ``merged`` — merged (terminal).
    ``failed`` — CI failed / merge rejected past retry (terminal).
    ``needs_resolution`` — a mergeability conflict; re-claimed so the poller can
    (once) re-dispatch the originating run to resolve it.
    ``abandoned`` — the run/deliverable/PR went away; stop watching (terminal).
    """

    PENDING_CI = "pending_ci"
    MERGING = "merging"
    MERGED = "merged"
    FAILED = "failed"
    NEEDS_RESOLUTION = "needs_resolution"
    ABANDONED = "abandoned"


class GithubMergeWatchRow(GithubMergeWatchBase):
    """One opened PR under CI-green auto-merge watch.

    ``run_id`` is the run whose delivery opened the PR — used later to
    re-dispatch on a merge conflict. ``conflict_dispatched`` is the loop guard
    for that re-dispatch (dispatch the resolving run at most once).
    """

    __tablename__ = "github_merge_watch"
    __table_args__ = (
        # The claim query: claimable rows whose poll time is due.
        Index("ix_github_merge_watch_status_next_poll", "status", "next_poll_at"),
        Index("ix_github_merge_watch_repo", "repo"),
        Index("ix_github_merge_watch_run", "run_id"),
        # One pull request, one watch row. ``(repo, pr_number)`` identifies a PR
        # permanently — GitHub never reuses a number within a repo — so a second
        # row is always a duplicate. A run that delivers twice finds the SAME
        # open PR, and two rows double the polling AND the founder's notices.
        Index("uq_github_merge_watch_repo_pr", "repo", "pr_number", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    deliverable_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    status: Mapped[MergeWatchStatus] = mapped_column(
        SAEnum(
            MergeWatchStatus,
            name="github_merge_watch_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=MergeWatchStatus.PENDING_CI,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conflict_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Conflict-robustness — how many times THIS conflict head has been
    #: re-dispatched to the agent. Reset to 1 on a fresh conflict (a first
    #: dispatch, or a new conflict on an advanced head); incremented on each
    #: deadline-driven retry. Bounds the retries: once it reaches
    #: ``github_conflict_max_redispatch`` an unresolved conflict escalates to a
    #: founder ``merge_conflict_review`` Decision instead of parking forever.
    conflict_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Conflict-robustness — when the CURRENT conflict was last (re-)dispatched.
    #: A ``needs_resolution`` re-poll on an UNCHANGED head computes ``now -
    #: conflict_dispatched_at`` against ``github_conflict_resolution_deadline_s``:
    #: within the deadline it keeps waiting (the agent may still be working);
    #: past it the re-drive is presumed stalled/failed, so the conflict is
    #: retried (bounded by ``conflict_attempts``) or escalated. Nullable — only
    #: set once a conflict has been dispatched.
    conflict_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    #: PR7 — the PR head SHA captured when a conflict was last re-dispatched to
    #: the agent. On a ``needs_resolution`` re-poll the worker compares it to the
    #: live head: an UNCHANGED head means the agent hasn't re-pushed yet (keep
    #: waiting, no re-dispatch); a CHANGED head means the agent produced a new
    #: state, so the freshness merge re-runs (clean → merge; still-conflict →
    #: reset the guard + re-dispatch once for the new head). Nullable — only set
    #: once a conflict has been dispatched.
    conflict_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )


__all__ = [
    "GithubMergeWatchBase",
    "GithubMergeWatchRow",
    "MergeWatchStatus",
]
